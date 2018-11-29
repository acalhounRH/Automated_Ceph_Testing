#!/usr/bin/bash +x

# this script implements the Jenkins job by the same name,
# just call it in the "Execute shell" field 
# in the job configuration
# because bash doesn't throw exceptions, 
# the script doesn't stop automatically because of a 
# fatal error, we have to check for errors and exit 
# with an error status if one is seen, 
# Jenkins will stop the pipeline immediately,
# this makes troubleshooting a pipeline much easier for the user

echo "test test test"
NOTOK=1   # process exit status indicating failure
systemctl stop firewalld 
systemctl stop iptables
script_dir=$HOME/automated_ceph_test
inventory_file=$HOME/ceph-linode/ansible_inventory


# clean house, so we don't have previous version of Ceph interfering
yum remove ceph-base ceph-ansible ansible librados2 -y
rm -rf /usr/share/ceph-ansible
rm -f /etc/ceph/ceph.conf /etc/ceph/ceph.client.admin.keyring
yum-config-manager --disable epel

cd $script_dir/staging_area/rhcs_latest/
new_ceph_iso_file="$(ls)"

rm -rf /ceph-ansible-keys
mkdir -m0777 /ceph-ansible-keys

#mount ISO and create rhcs yum repo file 
cp $script_dir/staging_area/repo_files/rhcs.repo /etc/yum.repos.d/
mkdir -p /mnt/rhcs_latest/
umount /mnt/rhcs_latest/
mount $script_dir/staging_area/rhcs_latest/RHCEPH* /mnt/rhcs_latest/
yum clean all

# install precise ansible version if necessary
if [ -n "$ansible_version" ] ; then
    yum install -y ansible-$ansible_version
fi

#install ceph-ansible
# python-rados is so check_cluster_status.py will work
yum install python-rados ceph-ansible -y

sed -i 's/gpgcheck=1/gpgcheck=0/g' /usr/share/ceph-ansible/roles/ceph-common/templates/redhat_storage_repo.j2

mkdir -p $script_dir/staging_area/tmp
echo "$ceph_ansible_all_config" > $script_dir/staging_area/tmp/all.yml
sed -i "s/<ceph_iso_file>/$new_ceph_iso_file/g" $script_dir/staging_area/tmp/all.yml
cp $script_dir/staging_area/tmp/all.yml /usr/share/ceph-ansible/group_vars/all.yml
echo "$ceph_ansible_osds_config" > $script_dir/staging_area/tmp/osds.yml
cp $script_dir/staging_area/tmp/osds.yml /usr/share/ceph-ansible/group_vars/osds.yml
ln -svf /usr/share/ceph-ansible/site.yml.sample /usr/share/ceph-ansible/site.yml

# set up python environment for linode API

cd $HOME/ceph-linode
echo "$Linode_Cluster_Configuration" > cluster.json
virtualenv-2 linode-env && source linode-env/bin/activate && pip install linode-python
export LINODE_API_KEY=$Linode_API_KEY
# careful, inventory file may not exist yet
export ANSIBLE_INVENTORY=$inventory_file


# if we have a version adjustment repo, use it
# use that before you try ceph-ansible
# this works around the lack of correct ansible version and/or
# and lack of latest versions of selinux-policy RPMs in centos/RHEL GA
# when a new RHCS version is released.
# the extra repo we insert is given by version_adjust_repo URL parameter
# also passed to preceding 1A job

if [ -n "$version_adjust_repo" ] ; then 
    # this is a hack to let launch.sh create linodes and ansible inventory
    # without actually running ceph-ansible

    mkdir -p /tmp/ceph-ansible/roles
    touch /tmp/ceph-ansible/site.yml
    # just create VMs
    /bin/bash +x ./launch.sh --ceph-ansible /tmp/ceph-ansible
    # ignore the error status, but make sure you have inventory
    if [ ! -f $ANSIBLE_INVENTORY ] ; then
        echo "failed to start linodes"
        exit $NOTOK
    fi
    version_adjust_name=`basename $version_adjust_repo`
    ln -svf ~/$version_adjust_name/version_adjust.repo /etc/yum.repos.d/
    yum clean all
    yum install -y libselinux-python || yum upgrade -y libselinux-python

    # to support ansible synchronize module
    yum install -y rsync
    ansible -m yum -a 'name=rsync' all

    # use ansible synchronize module to copy repo tree to all hosts
    # create softlink to it in /etc/yum.repos.d

    ansible -m synchronize -a "delete=yes src=~/$version_adjust_name dest=./" all
    ansible -m shell -a \
      "ln -svf ~/$version_adjust_name/version_adjust.repo /etc/yum.repos.d/" all

    # for some reason new RHCS selinux-ceph package
    # always has dependencies that the RHEL GA or centos 
    # version can't satisfy.

    ansible -m shell -a \
      'yum clean all ; yum install -y libselinux-python || yum upgrade -y libselinux-python' all \
      || exit $NOTOK
elif [ -f $ANSIBLE_INVENTORY ] ; then
    # remove any prior version adjustment repo
    (ansible -m file -a "path=/etc/yum.repos.d/version_adjust.repo state=absent" all && \
     ansible -m shell -a "yum clean all" all) || exit $NOTOK
fi

# run launch.sh again, this time with correct ceph-ansible path
# linode-launch.py will notice that the VMs have already been
# created and will not create any more.
# it will rerun pre-config.yml but this doesn't take very long

/bin/bash +x ./launch.sh --ceph-ansible /usr/share/ceph-ansible

# find out about first monitor

export first_mon=`ansible --list-host mons |grep -v hosts | grep -v ":" | head -1`
export first_mon_ip=`ansible -m shell -a 'echo {{ hostvars[groups["mons"][0]]["ansible_ssh_host"] }}' localhost | grep -v localhost`

# python-rados package must be installed on monitor 
# to run check_cluster_status.py there

ansible -m yum -a 'name=python-rados' $first_mon
ansible -m script -a "$script_dir/scripts/utils/check_cluster_status.py" $first_mon \
 || exit $NOTOK

# ceph-fuse enables cephfs testing using agent as a head node

yum install ceph-fuse ceph-common -y || exit $NOTOK

# make everyone in the cluster able to run ceph -s
# make agent able to access Ceph cluster as client

(scp $first_mon_ip:/etc/ceph/ceph.conf /etc/ceph/ && \
 scp $first_mon_ip:/etc/ceph/ceph.client.admin.keyring /etc/ceph/ && \
 ansible -m copy -a 'src=/etc/ceph/ceph.client.admin.keyring dest=/etc/ceph/' clients) \
 || exit $NOTOK

ceph -s

