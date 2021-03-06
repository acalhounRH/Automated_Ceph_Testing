#! /usr/bin/python

import os, sys, json, time, types
from time import gmtime, strftime
from elasticsearch import Elasticsearch
import statistics

def listdir_fullpath(d):
    return [os.path.join(d, f) for f in os.listdir(d)]


def import_into_elasticsearch(json_file, iteration, test_id):
    importdoc = {}
    header = {}
    importdoc['header'] = {}
    
    json_doc = json.load(open(json_file))
    #create header dict based on top level objects
    importdoc['date'] = time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.localtime(doc['timestamp']))
    importdoc['header']['global_options'] = json_doc['global options']
    importdoc['header']['global_options']['bs'] = ( int(importdoc['header']['global_options']['bs'].strip('B')) / 1024)
    importdoc['header']['timestamp_ms'] = json_doc['timestamp_ms']
    importdoc['header']['timestamp'] = json_doc['timestamp']
    importdoc['header']['fio_version'] = json_doc['fio version']
    importdoc['header']['time'] = json_doc['time']
    importdoc['header']['iteration'] = iteration
    importdoc['header']['test_id'] = test_id
    for i in xrange(len(json_doc['jobs'])):
        importdoc['write'] = json_doc['jobs'][i]['write']
        importdoc['read'] = json_doc['jobs'][i]['read']
        res = es.index(index="fio3-index", doc_type='fiojson', body=importdoc)

path = os.getcwd()
newdoc = {}
firsttime = 'false'
iteration_ary = []
op_ary = []
bs_ary = []
if len(sys.argv) > 3:
    test_id = sys.argv[1]
    host = sys.argv[2]
    esport = sys.argv[3]
else: 
    test_id = "librbdfio-" +  time.strftime('%Y-%m-%dT%H:%M:%SGMT', gmtime())
    
es = Elasticsearch(
        [host],
        scheme="http",
        port=esport,
     )

dirs = sorted(listdir_fullpath(path), key=os.path.getctime) #get iterations dir in time order
for cdir in dirs:
    if os.path.isdir(cdir):
        if os.path.basename(cdir) not in iteration_ary: iteration_ary.append(os.path.basename(cdir))
        test_dirs = sorted(listdir_fullpath(cdir), key=os.path.getctime) # get test dir in time order
        newdoc[os.path.basename(cdir)] = {}

        for test_dir in test_dirs:
            #print test_dir
            with open('%s/benchmark_config.yaml' % test_dir) as myfile: # open benchmarch_config.yaml and check if test is librbdfio 
                if 'benchmark: librbdfio' in myfile.read(): 
                    test_files = sorted(listdir_fullpath(test_dir), key=os.path.getctime) # get all samples from current test dir in time order
                    for file in test_files:
                        if "json_" in file:
                            doc = json.load(open(file))
               #             import_into_elasticsearch(file, os.path.basename(cdir), averdoc['test_id'])

                            if firsttime is 'false':
                                try: 
                                    newdoc[os.path.basename(cdir)][doc['global options']['rw']]
                                except:
                                    newdoc[os.path.basename(cdir)][doc['global options']['rw']] = {}
                                newdoc[os.path.basename(cdir)][doc['global options']['rw']][( int(doc['global options']['bs'].strip('B')) / 1024)] = {}
                                newdoc[os.path.basename(cdir)][doc['global options']['rw']][( int(doc['global options']['bs'].strip('B')) / 1024)]['date'] = time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.localtime(doc['timestamp']))
                                newdoc[os.path.basename(cdir)][doc['global options']['rw']][( int(doc['global options']['bs'].strip('B')) / 1024)]['write-iops'] = 0
                                newdoc[os.path.basename(cdir)][doc['global options']['rw']][( int(doc['global options']['bs'].strip('B')) / 1024)]['read-iops'] = 0
                                firsttime = 'true'
                        
                            if ( int(doc['global options']['bs'].strip('B')) / 1024) not in bs_ary: bs_ary.append(( int(doc['global options']['bs'].strip('B')) / 1024))
                            if doc['global options']['rw'] not in op_ary: op_ary.append(doc['global options']['rw'])
    
                            for i in xrange(len(doc['jobs'])):
                                 newdoc[os.path.basename(cdir)][doc['global options']['rw']][( int(doc['global options']['bs'].strip('B')) / 1024)]['write-iops'] += doc['jobs'][i]['write']['iops']
                                 newdoc[os.path.basename(cdir)][doc['global options']['rw']][( int(doc['global options']['bs'].strip('B')) / 1024)]['read-iops'] +=  doc['jobs'][i]['read']['iops']

                    firsttime = 'false'


#print json.dumps(newdoc, indent=1)
#for each operation and object size iterate over the # of test iteration of that specifc op / object_size and add it to a list
#after creating a list of IOPS for that specifc operation and object size calculate average and std deviation
for oper in op_ary:
    print oper
    for obj_size in bs_ary:
        print obj_size
        waver_ary = []
        raver_ary = []
        total_ary = []
        averdoc = {}
        averdoc['object_size'] = obj_size # set document's object size
        averdoc['test_id'] = test_id
        averdoc['operation'] = oper # set documents operation
        firstrecord = 'false'
        calcuate_percent_std_dev = False
        for itera in iteration_ary: # 
            try:
                waver_ary.append(newdoc[itera][oper][obj_size]['write-iops'])
                raver_ary.append(newdoc[itera][oper][obj_size]['read-iops'])

                if firstrecord is 'false':
                    averdoc['date'] = newdoc[itera][oper][obj_size]['date']
                    firstrecord = 'true'
            except:
                pass
        #print "##################average##################"
        read_average = (sum(raver_ary)/len(raver_ary))
        if read_average > 0.0:
            averdoc['read-iops'] = read_average
            if len(raver_ary) > 1:
                calcuate_percent_std_dev = True
        else:
            averdoc['read-iops'] = 0

        write_average = (sum(waver_ary)/len(waver_ary))
        if write_average > 0.0:
            print "process write %s" % len(waver_ary)
            averdoc['write-iops'] = write_average
            if len(waver_ary) > 1:
                calcuate_percent_std_dev = True 
        else:
	      	  averdoc['write-iops'] = 0

        averdoc['total-iops'] = (averdoc['write-iops'] + averdoc['read-iops'])
        

        if calcuate_percent_std_dev:
            if "read" in oper:
                averdoc['std-dev-%s' % obj_size] = round(((statistics.stdev(raver_ary) / read_average) * 100), 3)
            elif "write" in oper: 
                averdoc['std-dev-%s' % obj_size] = round(((statistics.stdev(waver_ary) / write_average) * 100), 3)
            elif "randrw" in oper:
                averdoc['std-dev-%s' % obj_size] = round((((statistics.stdev(raver_ary) + statistics.stdev(waver_ary)) / averdoc['total-iops'])* 100), 3)
        #print json.dumps(averdoc, indent=1) 
        res = es.index(index="cbt_librbdfio-summary-index", doc_type='fiologfile', body=averdoc)
        #print(res['result'])



