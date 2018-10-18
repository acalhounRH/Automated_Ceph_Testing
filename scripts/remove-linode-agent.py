import binascii
import logging
import os
import errno

from multiprocessing.dummy import Pool as ThreadPool

from contextlib import closing

import linode.api as linapi

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(levelname)s %(message)s')

GROUP = "Jenkins"

def main():
    key = os.getenv("LINODE_API_KEY")
    if key is None:
        raise RuntimeError("please specify Linode API key")

    client = linapi.Api(key = key, batching = False)

    def destroy(linode):
        client.linode_delete(LinodeID = linode[u'LINODEID'], skipChecks = 1)

    linodes = client.linode_list()
    logging.info("linodes: {}".format(linodes))

    with closing(ThreadPool(5)) as pool:
        group = filter(lambda linode: linode[u'LPM_DISPLAYGROUP'] == GROUP, linodes)
        pool.map(destroy, group)
        pool.close()
        pool.join()

    linodes = client.linode_list()
    logging.info("linodes: {}".format(linodes))

    # clear inventory file or else launch.sh won't create linodes

if __name__ == "__main__":
    main()
