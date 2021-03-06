
import time
import sys
import os
import pyshark
from pyshark.capture.pipe_capture import PipeCapture
sys.path.append("../lib")

from influxdb import InfluxDBClient
from optparse import OptionParser

# Logging
import simlogging
from simlogging import mconsole, logging
LOGNAME=__name__
LOGLEV = logging.INFO
LOGFILE="laptop_measure.log"
logger = simlogging.configureLogging(LOGNAME=LOGNAME,LOGFILE=LOGFILE,loglev = LOGLEV,coloron=False)

CLOUDLET_IP = '128.2.208.248'
CLOUDLET_PORT = 8086
UE_IP = '192.168.25.80'

TCP_DB = 'uetcp'
ICMP_DB = 'ueicmp'
FIFO_NAME = './uefifo'

CMD = "..\WinDump -i 4 -w - -U -s 0 icmp |python laptop_measure.py"
# Interface 4 when on JMA network with Multitech

# Command line processing
parser = OptionParser(usage="usage: %prog [options]")
parser.add_option("-f", "--fifo",
    action="store_true", dest="fifo", default=False,
    help="Use a named pipe (fifo) instead of stdin")
parser.add_option("-O", "--tcpoff",
    action="store_true", dest="tcpoff", default=False,
    help="Turn off tcp capture")
parser.add_option("-F", "--filename", dest="filename",
    help="take input from pcap file", metavar="FILE")

(options,args) = parser.parse_args()
kwargs = options.__dict__.copy()

# InfluxDB related initialization
tcp_client = InfluxDBClient(host=CLOUDLET_IP, port=CLOUDLET_PORT, database=TCP_DB)
tcp_client.alter_retention_policy("autogen", database=TCP_DB, duration="30d", default=True)

icmp_client = InfluxDBClient(host=CLOUDLET_IP, port=CLOUDLET_PORT, database=ICMP_DB)
icmp_client.alter_retention_policy("autogen", database=ICMP_DB, duration="30d", default=True)

# Filter for cloudlet packets to limit traffic to parse

if kwargs['fifo']:
    uefifo = os.open(FIFO_NAME, os.O_RDONLY) # Get from fifo
elif kwargs['filename'] is not None:
    uefifo = open(kwargs['filename'], 'r')
else:
    uefifo = sys.stdin

pipecap = PipeCapture(pipe=uefifo, debug=True, display_filter="ip.addr == {}".format(CLOUDLET_IP))

# Acceptable IP addresses to track for UE or cloudlet
IP_ADDR = ['192.168.25.52', CLOUDLET_IP, UE_IP,'172.26.25.174']

def log_packet(pkt):
    """
    Routine to execute for each packet received on the interface STDIN

    Extracts fields needed to correlate packets across each probe and insert into
    TCP or ICMP database
    """
    if "TCP" in pkt and not kwargs['tcpoff']:
        # print("logging TCP packet")
        try:
            tcp_timestamp = pkt.tcp.options_timestamp_tsval
        except:
            tcp_timestamp = 0

        try:
            tcp_ack = float(pkt.tcp.ack_raw)
        except:
            tcp_ack = 0

        # Adjust the packets if XRAN -> EPC packet
        pkt_entry = {"measurement":"latency", "tags":{"dst":pkt.ip.dst, "src":pkt.ip.src}, "fields":{"seqnum": int(pkt.tcp.seq_raw), "timestamp": int(tcp_timestamp), "acknum": float(tcp_ack), "epoch": float(pkt.frame_info.time_epoch)}}

        packets = []
        packets.append(pkt_entry)

        # Write to TCP database
        tcp_client.write_points(packets)
        

    elif "ICMP" in pkt:
        # print("1: ICMP SRC IP: {} DST IP: {}".format(pkt.ip.src,pkt.ip.dst))
        try:
            icmp_timestamp = str(pkt.icmp.data_time)
        except:
            icmp_timestamp = "NA"
            # print("No Timestamp")
            # return

        try:
            icmp_id = int(pkt.icmp.seq_le)
            icmp_seq = "{}/{}".format(str(pkt.icmp.seq),str(pkt.icmp.seq_le))
        except:
            return

        try:
            epoch = float(pkt.frame_info.time_epoch)
        except:
            return
        
        try:
            icmp_humantime = str(pkt.frame_info.time)
        except:
            return
        mconsole("Writing UE ICMP measurement -- SRC IP: {} DST IP: {} SEQUENCE: {}".format(pkt.ip.src,pkt.ip.dst,icmp_id))
        pkt_entry = {"measurement":"latency", "tags":{"dst":pkt.ip.dst, "src":pkt.ip.src}, 
                     "fields":{"data_time": icmp_timestamp, "epoch": epoch, 
                    "identifier": icmp_id, "sequence": icmp_seq, "htime": icmp_humantime}}
        # print(pkt_entry)
        packets = []
        packets.append(pkt_entry)

        # Write to ICMP database
        icmp_client.write_points(packets)

pipecap.apply_on_packets(log_packet)


