#!/bin/bash +x

# this script implements the Jenkins job by the same name,
# just call it in the "Execute shell" field 
# in the job configuration
# because bash doesn't throw exceptions, 
# the script doesn't stop automatically because of a 
# fatal error, we have to check for errors and exit 
# with an error status if one is seen, 
# Jenkins will stop the pipeline immediately,
# this makes troubleshooting a pipeline much easier for the user

script_dir=$HOME/automated_ceph_test
source /etc/profile.d/pbench-agent.sh

if [ "$linode_cluster" == "true" ]; then
	echo "setting up for linode"
	inventory_file=$HOME/ceph-linode/ansible_inventory_tmp
    > $HOME/.ssh/config
else
	inventory_file=$script_dir/ansible_inventory
fi 


function add_to_sshconfig {
	# if user have predefined users, it is their responsibility to setup ssh keys user register_host.sh
	if [ "$linode_cluster" == "true" ]; then
		new_config="
		Host $1
	  		User root
	  		StrictHostKeyChecking no
		"
	
		echo "$new_config" >> $HOME/.ssh/config
	fi 
}

function register_tools {

	if [ "$sar" = "true" ]; then
        /opt/pbench-agent/util-scripts/pbench-register-tool --name=sar --remote=$1 default -- --interval=$sar_interval
    fi
    if [ "$iostat" = "true" ]; then
        /opt/pbench-agent/util-scripts/pbench-register-tool --name=iostat --remote=$1 default -- --interval=$iostat_interval
    fi
    if [ "$mpstat" = "true" ]; then
        /opt/pbench-agent/util-scripts/pbench-register-tool --name=mpstat --remote=$1 default -- --interval=$mpstat_interval        
    fi
    if [ "$pidstat" = "true" ]; then
        /opt/pbench-agent/util-scripts/pbench-register-tool --name=pidstat --remote=$1 default -- --interval=$pidstat_interval        
    fi
    if [ "$proc-vmstat" = "true" ]; then 
        /opt/pbench-agent/util-scripts/pbench-register-tool --name=proc-vmstat --remote=$1 default -- --interval=$proc_vmstat_interval        
    fi
    if [ "$proc-interrupts" = "true" ]; then
        /opt/pbench-agent/util-scripts/pbench-register-tool --name=proc-interrupts --remote=$1 default -- --interval=$proc_interrupts_interval        
    fi
    if [ "$turbostat" = "true" ]; then
        /opt/pbench-agent/util-scripts/pbench-register-tool --name=turbostat --remote=$1 default -- --interval=$turbo_interval       
    fi
    
    #for tool_name in $tools_ary; do
    #	/opt/pbench-agent/util-scripts/pbench-register-tool --name=$tool_name --remote=$1 default -- --interval=3
    #done
}

service_inventory="
[servers:children]
osds
mons
mgrs
clients
"

if [ "$linode_cluster" == "true" ]; then
	echo "copy linode inventory"
	cp $HOME/ceph-linode/ansible_inventory $inventory_file
fi

echo "$service_inventory" >> $inventory_file

#Setup and install pbench on all linode host
cat $inventory_file

	yum install ceph-common -y
	ansible -m fetch -a "src=/etc/ceph/ceph.conf dest=/etc/ceph/ceph.conf.d" client-000 -i $inventory_file
    cp /etc/ceph/ceph.conf.d/client-000/etc/ceph/ceph.conf /etc/ceph/ceph.conf
    ceph_client_key=/ceph-ansible-keys/`ls /ceph-ansible-keys/ | grep -v conf`/etc/ceph/ceph.client.admin.keyring
	cp $ceph_client_key /etc/ceph/ceph.client.admin.keyring
    cd /etc/yum.repos.d
    yum install https://dl.fedoraproject.org/pub/epel/epel-release-latest-7.noarch.rpm -y 
    yum install -y yum-utils; yum-config-manager --enable epel
	wget https://copr.fedorainfracloud.org/coprs/ndokos/pbench/repo/epel-7/ndokos-pbench-epel-7.repo; yum install pbench-agent -y
    yum install pbench-fio -y
	yum install pdsh -y
	echo "******************* install pbench agent in linode"
	ansible -m shell -a "yum install wget -y; cd /etc/yum.repos.d; wget https://copr.fedorainfracloud.org/coprs/ndokos/pbench/repo/epel-7/ndokos-pbench-epel-7.repo; yum install pbench-agent -y" -i $inventory_file all
    echo "*******************install pbench-fio and pdsh for CBT"
	ansible -m shell -a "yum install https://dl.fedoraproject.org/pub/epel/epel-release-latest-7.noarch.rpm -y; yum install -y yum-utils; yum-config-manager --enable epel; yum install pbench-fio -y; yum install pdsh -y;" all -i $inventory_file
    
ansible -m copy -a "src=/etc/ceph/ceph.client.admin.keyring dest=/etc/ceph/ceph.client.admin.keyring" clients -i $inventory_file


if [ ! -d /var/lib/pbench-agent/tools-default-old ]; then
  sudo mkdir -p /var/lib/pbench-agent/tools-default-old;
fi

echo "******************* making tools dir"
sudo mv /var/lib/pbench-agent/tools-default/* /var/lib/pbench-agent/tools-default-old/
sudo chmod 777 /var/lib/pbench-agent/tools-default/
sudo chmod 777 /var/lib/pbench-agent/pbench.log 

echo "******************* registering tools:"
for i in `ansible --list-host -i $inventory_file all |grep -v hosts | grep -v ":"`
        do
        		echo "testing $i"
                ping $i -c 1 > /dev/null 2>&1
                exit_status=`echo $?`
                if [ "$exit_status" -gt "0" ]; then
                		echo "failed ping on $i"
                		add_to_sshconfig `cat $inventory_file | grep $i | awk {'print $2'} | sed 's/.*=//'`
                		register_tools `cat $inventory_file | grep $i | awk {'print $2'} | sed 's/.*=//'`
                elif [ "$exit_status" -eq "0" ]; then
                		echo "pinged $i"
                		add_to_sshconfig $i
                		register_tools $i
                else
                        echo "Unable to register tools on $i"
                fi
done
