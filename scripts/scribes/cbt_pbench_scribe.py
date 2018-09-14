
import yaml, os, time, json, hashlib, sys
import socket, datetime, csv, logging

logger = logging.getLogger("index_cbt")

class pbench_transcriber:

    def __init__(self, csv_file, metadata):
        self.csv_file = csv_file
        self.metadata = metadata
        
    def debug_progress_logger(text):
        current_log_level = logger.level
        print current_log_level
        if "debug" in current_log_level:
            logger.debug(text)
            print '\x1b[80D' + '\x1b[K'+ text,
            sys.stdout.flush()

    def emit_actions(self):
        importdoc = {}
        importdoc["_index"] = "pbenchtest1"
        importdoc["_type"] = "pbenchdata"
        importdoc["_op_type"] = "create"
        importdoc['_source'] = self.metadata
        
        logger.debug("Indexing %s" % self.csv_file)
        file_progress = 0
        total_progress = 0
        
        with open(self.csv_file) as csvfile:
            readCSV = csv.reader(csvfile, delimiter=',')
            for line in readCSV:
                total_progress += 1
                
        with open(self.csv_file) as csvfile:
            readCSV = csv.reader(csvfile, delimiter=',')
            first_row = True
            col_ary = []
            
            for row in readCSV:
                if first_row:
                    col_num = len(row)
                    for col in range(col_num):
                        col_ary.append(row[col])
                        first_row = False
                else:
                    for col in range(col_num):
                        a = {}
                        #importdoc['_source']['test_data'] = {}      #remove
                        tool = importdoc['_source']['ceph_benchmark_test']['common']['test_info']['tool']         
                        file_name = importdoc['_source']['ceph_benchmark_test']['common']['test_info']['file_name']
                        file_name = file_name.split('.',1)[0]
                        
                        #importdoc['_source']['test_data'][tool] = {}
                        #importdoc['_source']['test_data'][tool][file_name] = {}
                        
                        tmp_doc = {
                            tool: {
                                file_name: {}
                                }
                            }
                        
                        if 'timestamp_ms' in col_ary[col]:
                            ms = float(row[col])
                            thistime = datetime.datetime.fromtimestamp(ms / 1000.0)
                            importdoc['_source']['date'] = thistime.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
                        else:
                            if 'pidstat' in tool:
                                node_type_list = ["ceph-mon", "ceph-osd", "ceph-mgr", "ceph-mds", "ceph-rgw"]
                                pname = col_ary[col].split('/')[-1]
                                
                                for node_type in node_type_list:
                                    if  node_type in pname:    
                                        pid = col_ary[col].split('-', 1)[0]
                                        tmp_doc[tool][file_name]['process_name'] = node_type
                                        tmp_doc[tool][file_name]['process_pid'] = pid
                                        tmp_doc[tool][file_name]['metric_value'] = float(row[col])
                                        a = importdoc
                            else:
                                tmp_doc[tool][file_name]['metric_stat'] = col_ary[col]
                                tmp_doc[tool][file_name]['metric_value'] = float(row[col])
                                a = importdoc
                        if a:
                                importdoc["_source"]['ceph_benchmark_test']["test_data"] = tmp_doc
                                importdoc["_id"] = hashlib.md5(json.dumps(importdoc)).hexdigest()
                                yield a
#                     current_progress = ((file_progress - total_progress) / total_progress) * 100 
#                     debug_progress_logger("indexing... %s" % current_progress)
#                     file_progress += 1
                    
                    