# Automated Ceph Test

Combination of Jenkins, ceph benchmarking tools and scripts that enable a user to automate the testing of ceph

## Summary:

![alt text](https://github.com/acalhounRH/automated_ceph_test/blob/master/docs/pictures/applicaton_deployment.png)
![alt text](https://github.com/acalhounRH/automated_ceph_test/blob/master/docs/pictures/Automated_Test_Data_Flow.png)

## Installation:

In order to install first pull automated ceph test, git clone https://github.com/acalhounRH/automated_ceph_test.git
# Jenkins Controller (Master) Setup
On the Control node navigate to the automated_ceph_test directory and run setup.sh -m. setup will install necessary dependencies and launch the jenkins master. 
# Jenkins Agent
Agent deployment is controled via the jenkins job prepare_agent.

## How to Run:

## Expected Results:

## Dependencies:

- ceph_ansible
- Ceph Benchmark Tool (CBT)
- ceph_linode
- smallfile
- cosbench
- Elasticsearch 
- Grafana 

Note: elasticsearch and grafana are **NOT** installed/setup using automated_ceph_test, and should be insalled/setup prior to automated_ceph_test.