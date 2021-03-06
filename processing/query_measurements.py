#!/usr/bin/env python

import sys

sys.path.append("../lib")
import os
from influxdb import InfluxDBClient, DataFrameClient
import numpy as np
import pandas as pd
import seaborn as sns
import matplotlib.pyplot as plt
import time
from pyutils import *
from pdutils import *
from pdpltutils import *

from optparse import OptionParser
import simlogging
from simlogging import mconsole
''' 
    This program collects latency measurements from the UE, waterspout and cloudlet influxdb databases,
    aggregates them and writes them back into the segmentation database for later analysis and dashboard dataset
'''
LOGNAME=__name__
LOGLEV = logging.INFO

''' Hardcode cloudlet IP and port for DB '''
CLOUDLET_IP = '128.2.208.248'
EPC_IP = '192.168.25.4'
CLOUDLET_PORT = 8086
TZ = 'America/New_York'

''' Influxdb databases ''' 
CLOUDLET_ICMP_DB = 'cloudleticmp'
WATERSPOUT_ICMP_DB = 'waterspouticmp'
UE_ICMP_DB = 'ueicmp'
SEG_DB = 'segmentation'
OFFSET_DB = 'winoffset'

''' For labeling segments '''
legdict = {1: 'ue_xran',2:'xran_epc',3:'epc_cloudlet',
           7: 'ue_xran',6:'xran_epc',5:'epc_cloudlet',0:'start',4:'cloudlet_proc'}

''' For bad sequences '''
blacklist = []

def main():
    global logger
    global blacklist
    
    ''' Logging '''
    LOGFILE="query_measurments.log"
    (options,_) = cmdOptions()
    kwargs = options.__dict__.copy()
    loglev = LOGLEV if not kwargs['debug'] else logging.DEBUG
    logger = simlogging.configureLogging(LOGNAME=LOGNAME,LOGFILE=LOGFILE,loglev = loglev,coloron=False)
    
    def noMeasurements(fdf,tag='no info'):
        if len(fdf) == 0:
            mconsole("No measurements available for the segmentation database: {}".format(tag))
            return True
        else: return False
        
    ''' Get the database client '''
    seg_client = InfluxDBClient(host=CLOUDLET_IP, port=CLOUDLET_PORT, database=SEG_DB)
    firsttime = True
    
    ''' Now keep looping '''
    while True:
        if not firsttime: time.sleep(int(kwargs['sleeptime']))
        firsttime = False
        
        ''' Pull the data from the cloudlet, waterspout and ue databases '''
        try:
            latencydf = getLatencyData()
        except:
            mconsole("Did not get any latency data",level = "ERROR")
            continue
        
        ''' Is it already in the segmentation database? '''
        latencydf = checkAgainstCurrent(latencydf)
        if noMeasurements(latencydf,tag="no new"): continue
        else: mconsole("Retrieved {} possible sequences (Sequence Nos: {})" \
                       .format(len(list(set(latencydf.sequence))),list(set(latencydf.sequence))),level="DEBUG")
        fullseqlst = list(set(latencydf.sequence))
        ''' Group the sequences and clean up '''
        tdfz = latencydf.copy().sort_values(['sequence','TIMESTAMP'])
        
        ''' Make a small df that has just the number of times a sequence appears in the df '''
        tdfz = renamecol(tdfz.groupby(by='sequence') \
                .agg('count').reset_index()[['sequence','TIMESTAMP']],col='TIMESTAMP',newname='COUNT')
        
        ''' Join the small df with bigger '''
        tdfz = tdfz.set_index('sequence').join(latencydf.set_index('sequence')) \
                .reset_index().sort_values(['sequence','STEP'])
        tdfz = tdfz.drop_duplicates(subset = ['sequence','NAME','epoch'])
        
        ''' Check for complete sequence data from all probes '''
        preseq = list(set(tdfz.sequence))
        ''' Remove partial sequences '''
        tdfz = tdfz[tdfz.COUNT >= 8] 
        
        ''' Add partial sequences to the blacklist '''
        if noMeasurements(tdfz,tag="partial data"): 
            mconsole("Sequence(s) {} added to blacklist".format(preseq),level = "DEBUG")
            blacklist += preseq
            continue
        else: mconsole("Retrieved {} possibly complete sequences (Sequence Nos: {})" \
                       .format(len(list(set(tdfz.sequence))),list(set(tdfz.sequence))),level="DEBUG") # 2 for uplink and downlink
        
        ''' Calculate difference between each step ; save the epoch from the UE uplink as the start of the sequence '''
        tdfz['DELTA'] = tdfz.epoch - tdfz.epoch.shift(1)
        tdfz['DELTA'] = tdfz.apply(lambda row: row['epoch'] if 'ue' in row.NAME and 'uplink' in row.direction else row.DELTA, axis=1)
        
        ''' Calculate the round trip time (RTT) two different ways '''
        '''RTT and STEPSUM should be the same; RTT is the (end - start) at the UE; STEPSUM is the sum of the DELTAS '''
        tdfz['RTT'] =  tdfz.epoch - tdfz.epoch.shift(7)
        tdfz['RTT'] = tdfz.apply(lambda row: row['RTT'] \
                        if 'ue' in row.NAME and 'downlink' in row.direction else np.nan, axis=1).fillna(method='bfill')
        tdfz['STEPSUM'] = tdfz.DELTA.rolling(min_periods=1, window=7).sum()
        tdfz['STEPSUM'] = tdfz.apply(lambda row: row['STEPSUM'] \
                        if 'ue' in row.NAME and 'downlink' in row.direction else np.nan, axis=1).fillna(method='bfill')

        ''' Convert the DF for storage in the segmentation database '''
        indlst = ['sequence','direction','RTT','STEPSUM']
        keepcol = ['sequence','epoch','TIMESTAMP','NAME','direction','DELTA','STEP','LEGNAME','STEPSUM','RTT']
        tdfx = tdfz.copy()[tdfz.COUNT >= 8][keepcol].sort_values(['sequence','STEP']) \
                        .reset_index(drop=True).drop_duplicates()

        ''' Pivot the data '''
        tdfx = tdfx.drop_duplicates(subset = indlst + ['LEGNAME'])
        debugdf = tdfx.copy()
        tdfx = tdfx.pivot(index=indlst, columns=['LEGNAME'], values=['DELTA']).reset_index()
        ''' Get rid of multilevel index '''
        tdfx.columns = ["".join(a).replace("DELTA","") for a in tdfx.columns.to_flat_index()]
        if noMeasurements(tdfx,tag="nothing after pivot") or "start" not in tdfx.columns: continue
        ''' Propagate the start time to the downlink '''
        tdfx = tdfx.sort_values(['sequence','direction'],ascending=[True,False])
        try: 
            tdfx['start'] = tdfx.start.fillna(method='ffill').dropna()
        except:
            mconsole("No start field in data frame",level="ERROR")
            continue
        tdfx = tdfx.dropna(subset=['start'])
        tdfx['DTDATE'] = tdfx.start.map(lambda cell: datetime.datetime.fromtimestamp(cell,tz=pytz.timezone(TZ)))
        
        ''' Find the offset '''
        tdfx['offset'] = getOffset(startts = tdfx.start.min(),endts = tdfx.start.max(),filteroffset=kwargs['filteroffset'])

        ''' adjust the ue_xran latencies to correct for offset'''
        tdfx['ue_xran_adjust'] = tdfx.apply(lambda row: row.ue_xran - row.offset \
                                            if row.direction == 'uplink' else row.ue_xran + row.offset, \
                                            axis=1)
        
        ''' Cleanup the cloudlet processing time '''
        tdfx['cloudlet_proc'] = tdfx.cloudlet_proc.map(lambda col: col if not np.isnan(col) else 0)
        
        ''' Write to the segmentation database '''
        tdfx = tdfx.dropna()
        blacklist += list(set(fullseqlst) - set(tdfx.sequence) - set(blacklist))
        mconsole("Writing {} measurements to the segmentation database  (Sequence Nos: {})" \
                 .format(len(tdfx),list(set(tdfx.sequence))))
        tdfx[:].apply(writePkt,client = seg_client, axis=1)
        
def cmdOptions():
    parser = OptionParser(usage="usage: %prog [options]")
    parser.add_option("-D", "--delay", dest="sleeptime",
              help="Delay between queries", metavar="INT",default=5)
    parser.add_option("-d", "--debug",
                  action="store_true", dest="debug", default=False,
                  help="Debugging mode")
    parser.add_option("-f", "--filteroffset",
                  action="store_true", dest="filteroffset", default=False,
                  help="Filter out data without nearby offset values")
    return  parser.parse_args()
    
def checkAgainstCurrent(newdf):
    ''' Is the sequence already in the database '''
    df_seg_client = DataFrameClient(host=CLOUDLET_IP, port=CLOUDLET_PORT, database=SEG_DB)
    measure = 'uplink'
    try:
        seg_ul_df = df_seg_client.query("select * from {}".format(measure))[measure]
    except:
        return newdf
    seqlst = list(set(seg_ul_df.sequence))
    return newdf[~newdf.sequence.isin(seqlst)]

def checkAgainstBlacklist(indf):
    retdf = indf[~indf.sequence.isin(blacklist)]
    return retdf

def getLatencyData():
    ''' Get all the clients '''
    df_cloudlet_icmp_client = DataFrameClient(host=CLOUDLET_IP, port=CLOUDLET_PORT, database=CLOUDLET_ICMP_DB)
    df_waterspout_icmp_client = DataFrameClient(host=CLOUDLET_IP, port=CLOUDLET_PORT, database=WATERSPOUT_ICMP_DB)
    df_ue_icmp_client = DataFrameClient(host=CLOUDLET_IP, port=CLOUDLET_PORT, database=UE_ICMP_DB)
                                        
    ''' Query the different network nodes' data '''
    measure = 'latency'
    cloudlet_icmp_df = df_cloudlet_icmp_client.query("select * from {}".format(measure))[measure]
    waterspout_icmp_df = df_waterspout_icmp_client.query("select * from {}".format(measure))[measure]
    ''' Eliminate duplicate waterspout packets -- fix for XRAN double reporting with different epochs '''
    waterspout_icmp_df = waterspout_icmp_df.groupby(by=['sequence','src','dst']).agg({'epoch':'min'}) \
                .sort_values('epoch').reset_index()
    
    ue_icmp_df = df_ue_icmp_client.query("select * from {}".format(measure))[measure]
    
    ''' Get list of sequences in all three dataframes '''
    seqminset = list(set(ue_icmp_df.sequence) \
        .intersection((set(cloudlet_icmp_df.sequence) \
        .intersection(set(waterspout_icmp_df.sequence)))))
    
    ''' Combine the nodes '''
    dflst = [(cloudlet_icmp_df,'cloudlet'),(waterspout_icmp_df,'waterspout'),(ue_icmp_df,'ue')]
    tdfy = pd.DataFrame()
    for tup in dflst:
        tdfx = tup[0]
        tdfx['NAME'] = tup[1]
        tdfy = tdfy.append(tdfx)
    
    ''' Only keep sequences that are in all three dataframes '''
    tdfy = tdfy[tdfy.sequence.isin(seqminset)]
    
    ''' Only keep sequences that are not on blacklist '''
    tdfy = checkAgainstBlacklist(tdfy)
    
    ''' Label the segments in what remains '''
    
    tdfy['TIMESTAMP']= pd.to_datetime(tdfy['epoch'],unit='s',utc=True) # convenience
    tdfy = changeTZ(tdfy,col='TIMESTAMP',origtz='UTC', newtz=TZ)
    tdfy[['direction','STEP','LEGNAME']] = tdfy.apply(lookupLeg,axis=1, result_type='expand')
    tdfy = renamecol(tdfy.reset_index().copy(),col='index',newname='influxts')
    
    return tdfy

def getOffset(startts = None, endts = None, measure = 'winoffset',filteroffset = True):
    TZ = 'America/New_York'
    df_offset_client = DataFrameClient(host=CLOUDLET_IP, port=CLOUDLET_PORT, database=OFFSET_DB)
    offset_df = to_ts_std(df_offset_client.query("select * from {}".format(measure))[measure],newtz='America/New_York')
    ''' Filter by ts boundaries '''
    tdfx = offset_df.copy()
    ''' convert from epoch to datetime '''
    startdt = datetime.datetime.fromtimestamp(startts,tz=pytz.timezone(TZ)) if startts is not None else None
    enddt = datetime.datetime.fromtimestamp(endts,tz=pytz.timezone(TZ)) if endts is not None else None
    ''' remove out of bounds '''
    if filteroffset:
        tdfx = tdfx[tdfx.TIMESTAMP >= startdt] if startts is not None else tdfx
        tdfx = tdfx[tdfx.TIMESTAMP <= enddt] if endts is not None else tdfx
    if len(tdfx) < len(offset_df):
        mconsole("Filtered offset to boundaries: {} to {}".format(startdt,enddt))
    median_offset = tdfx.offset.median()
    return median_offset


''' Label the legs '''
def lookupLeg(row):
    src = row.src
    dst = row.dst
    name = row.NAME
    ddir = np.nan
    step = np.nan
    if dst == CLOUDLET_IP:
        ddir = "uplink"        
    elif src == CLOUDLET_IP:
        ddir = "downlink"
    elif dst == EPC_IP:
        ddir = "uplink"
    elif src == EPC_IP:
        ddir = "downlink"
    if ddir == 'uplink':
        if name == 'ue': step = 0
        elif name == 'cloudlet': step = 3
        elif name == 'waterspout': step = 1 if dst == EPC_IP else 2                
    elif ddir == 'downlink':
        if name == 'ue': step = 7
        elif name == 'cloudlet': step = 4
        elif name == 'waterspout': step = 5 if dst == EPC_IP else 6
    legname = legdict[step]
    return (ddir,step,legname)

''' Write into influxdb '''
def writePkt(row, client):
        mconsole("Writing measurement -- sequence={} direction={} start={}".format(row.sequence,row.direction,row.start),level="DEBUG")
    # try:
        pkt_entry = {"measurement":row['direction'], 
                     "tags": {"sequence": row['sequence'],"direction": row['direction']}, 
                     "fields":{"ue_xran": row['ue_xran'], 'ue_xran_adjust':row['ue_xran_adjust'],
                               'offset':row['offset'],"xran_epc": row['xran_epc'], 
                                "epc_cloudlet": row['epc_cloudlet'], "start": row['start'],
                                "cloudlet_proc": row['cloudlet_proc'],
                                'rtt':row.RTT,'stepsum':row.STEPSUM},"time":int(row['start']*1e6)}
        client.write_points([pkt_entry], time_precision = 'u')
    # except:
    #     mconsole("Bad measurement: {}".format(row),level="ERROR")

if __name__ == '__main__': main()