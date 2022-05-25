cont_name=fairshell-unbound

all:
	@./get_ads_list.sh
	sudo docker rmi $(cont_name) | true
	sudo docker build --no-cache -t $(cont_name) .
	sudo docker save $(cont_name) > ../$(cont_name).tar
