# this script is executed as a plugin by the unbound server
import os
import json
import platform
import tempfile
import time
import datetime

forward_zones_file="/etc/forward-zones.json"
resolved_dir="/resolved"
logs_dir="/logs"

denied_file=open("%s/denied.log"%logs_dir, "ab", buffering=0)
resolved_file=open("%s/resolved.log"%logs_dir, "ab", buffering=0)

forward_zones=[]

def get_timestamp():
    """Get the current Unix timestamp as UTC"""
    now=datetime.datetime.utcnow()
    return int(datetime.datetime.timestamp(now))

def init(id, cfg):
    #log_info("pythonmod: init called, module id is %d port: %d script: %s" % (id, cfg.port, cfg.python_script))
    global forward_zones

    log_info("Python (version %s) module init"%platform.python_version())
    forward_zones=json.loads(open(forward_zones_file, "r").read())

    # remove any stale file
    for fname in os.listdir(resolved_dir):
        path="%s/%s"%(resolved_dir, fname)
        try:
            os.remove(path)
        except Exception:
            pass
    return True

def deinit(id):
    return True

def inform_super(id, qstate, superqstate, qdata):
    return True

def get_requester_ip(qstate):
    # determine source IP address
    #   -> see https://adamo.wordpress.com/2018/07/06/unbound-python-and-conditional-replies-based-on-source-ip-address/
    try:
        rl=qstate.mesh_info.reply_list
        q=None
        while rl:
            if rl.query_reply:
                q=rl.query_reply
                break
            rl=rl.next
        if q:
            return q.addr
        return None
    except NameError as e:
        addr = "[ERR: %s]"%str(e)

def get_A_record(data):
    (rdlength, rdata) = (data[:2], data[2:])
    try:
        assert rdlength==b'\x00\x04'
        assert len(rdata)==4
        addr_str=[str(c) for c in rdata]
        return ".".join(addr_str)
    except Exception as e:
        log_info("Unhandled A record data %s"%data)
        return None

def get_AAAA_record(data):
    try:
        (rdlength, rdata) = (data[:2], data[2:])
        #assert rdlength==b'\x00\x10'
        #assert len(rdata)==16
        addr_bytes = [c for c in rdata]
        addr_str=[]
        for index in range(0,8):
            if addr_bytes[index]==0:
                data="%x"%addr_bytes[index+1]
            else:
                data="%x%02x"%(addr_bytes[index],addr_bytes[index+1])
            addr_str+=[data]
        return ":".join(addr_str)
    except Exception as e:
        log_info("Unhandled AAAA record data %s"%data)
        return None

def operate(id, event, qstate, qdata):
    global forward_zones
    #log_info("pythonmod: operate called, id: %d, event:%s" % (id, strmodulevent(event)))

    if event in (MODULE_EVENT_NEW, MODULE_EVENT_PASS):
        if qstate.qinfo.qclass==RR_CLASS_IN:
            try:
                # determine source IP address
                req_addr=get_requester_ip(qstate)
                if req_addr and qstate.qinfo.qtype_str in ("A", "AAAA"):
                    # filter only for queries coming from the outside world of unbound
                    log_info("CHECK query '%s' from '%s'"%(qstate.qinfo.qname_str, req_addr))

                    # test query validity
                    allowed=False
                    qname=qstate.qinfo.qname_str[:-1]
                    if qname=="smb.local":
                        allowed=True
                    else:
                        for zone in forward_zones:
                            if qname.endswith(zone):
                                allowed=True
                                break
                    if not allowed:
                        now=get_timestamp()
                        data="%s %s FROM %s\n"%(now, qname, req_addr)
                        denied_file.write(data.encode())
                        raise Exception ("'%s' is NOT ALLOWED"%qname)

                    # Pass on the new event to the iterator
                    qstate.ext_state[id]=MODULE_WAIT_MODULE
                    return True
                else:
                    log_info("NOCHECK for '%s' (type '%s')"%(qstate.qinfo.qname_str, qstate.qinfo.qtype_str))
                    qstate.ext_state[id]=MODULE_WAIT_MODULE
                    return True
            except Exception as e:
                log_err("ERROR while handling event %s: %s"%(strmodulevent(event), str(e)))
                qstate.ext_state[id]=MODULE_ERROR
                return True
        else:
            log_info("Unhandled qstate class '%s'"%qstate.qinfo.qclass)
            qstate.ext_state[id]=MODULE_WAIT_MODULE
            return True

    elif event==MODULE_EVENT_MODDONE:
        # determine source IP address
        req_addr=get_requester_ip(qstate)

        log_info("RESPONSE '%s' for query from '%s'"%(qstate.qinfo.qname_str, req_addr))
        if qstate.return_msg:
            try:
                # build list of resolved IPs
                r = qstate.return_msg.rep
                resolved_ips=[]
                for i in range(0, r.rrset_count):
                    rr = r.rrsets[i]
                    rk = rr.rk

                    if rk.rrset_class_str=="IN":
                        if rk.type_str=="A":
                            d = rr.entry.data
                            for j in range(0,d.count+d.rrsig_count):
                                ttl=d.rr_ttl[j]
                                rec=get_A_record(d.rr_data[j])
                                if rec:
                                    log_info("RESOLVED %s =A=> %s"%(qstate.qinfo.qname_str, rec))
                                    resolved_ips+=[{"TTL": ttl, "A": rec, "AAAA": None}]
                                # TODO: report on the d.security and d.trust values
                        elif rk.type_str=="AAAA":
                            d = rr.entry.data
                            for j in range(0,d.count+d.rrsig_count):
                                ttl=d.rr_ttl[j]
                                rec=get_AAAA_record(d.rr_data[j])
                                if rec:
                                    log_info("RESOLVED %s =AAAA=> %s"%(qstate.qinfo.qname_str, rec))
                                    resolved_ips+=[{"TTL": ttl, "A": None, "AAAA": rec}]

                if len(resolved_ips)>0:
                    # log the resolution to a TMP file so the host can monitor it and modify the FW rules
                    # accordingly
                    now=get_timestamp()
                    res_str=json.dumps(resolved_ips)
                    data="%s %s %s\n"%(now, res_str, qstate.qinfo.qname_str)
                    resolved_file.write(data.encode())

                    (tmpfd, tmpname)=tempfile.mkstemp(dir=resolved_dir)
                    os.write(tmpfd, res_str.encode())
                    os.close(tmpfd)
                    time.sleep(0.2) # leave some time to propagate
            except Exception as e:
                log_err("ERROR while handling response: %s"%str(e))

        qstate.ext_state[id]=MODULE_FINISHED
        return True
    else:
        log_err("Unhandled event %s"%event)
        qstate.ext_state[id]=MODULE_ERROR
        return True
