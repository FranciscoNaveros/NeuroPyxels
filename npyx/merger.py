# -*- coding: utf-8 -*-
"""
2019-06-13
@author: Maxime Beau, Neural Computations Lab, University College London

Dataset merger class: represents a collection of Neuropixels Datasets (no theoretical upper limit on number of probes)
as a networkx network
with nodes being characterized as cell types
and edges as putative connections.

The way to use it is as follow:

# initiation
dp_dic = {'probe1': 'path/to/dataset1', 'probe2':'path/to/dataset2', ...}
merged = Merger(dp_dic)

# Connect the graph with putative monosynaptic connections
merged.connect_graph()

# Plot the graph
merged.plot_graph()

# Get N of putative connections of a given node spotted on the graph
merged.get_node_edges(node)

# Only keep a set of relevant nodes or edges and plot it again
merged.keep_edges(edges_list)
merged.keep_nodes_list(nodes_list)
merged.plot_graph()

# every graph operation of dataset merger can be performed on external networkx graphs
# provided with the argument 'src_graph'
g=merged.get_graph_copy(mergerGraph='undigraph')
merged.keep_nodes_list(nodes_list, src_graph=g) # g itself will be modified, not need to do g=...
merged.plot_graph(graph_src=g)


"""


import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)

import os
from ast import literal_eval as ale
import operator
import time
import imp
import os.path as op; opj=op.join
from pathlib import Path

import matplotlib.pyplot as plt
from mpl_toolkits.axes_grid1.inset_locator import inset_axes
import numpy as np
import pandas as pd

import npyx
from npyx.utils import npa, sign, align_timeseries

from npyx.io import read_spikeglx_meta, get_npix_sync, chan_map
from npyx.gl import load_units_qualities
from npyx.spk_wvf import get_depthSort_peakChans, get_peak_chan
from npyx.corr import gen_sfc

import networkx as nx

class Merger:
    '''
    Represents a collection of Neuropixels Datasets (no theoretical upper limit on number of probes)
    as a networkx network, with nodes being characterized as cell types
    and edges as putative connections.

    Initially designed to explore graph properties of networks,
    it is mainly used to simply merge datasets together and align data accross probes!

    Any npyx function will run on a merged dataset just as on a regular dataset directory.

    --------- Initialization -> simply merge datasets together ---------

    Usage for single probe recording:
    >>> dp = path/to/kilosort/output
    >>> merged = Merger(dp)

    Structure of merged: merged generated a networkx graph whose nodes correspond to the dataset units labeled as good.
    Bear in mind that the graph nodes are actual objects, instances of Unit(), making this structure more powerful
    Because relevant methods can be integrated either in the Dataset class (connections related stuff) or the Units class (individual units related stuff)

    If you want to access unit u:
    >>> merged.units[u]
    >>> merged.units[u].trn(): equivalent to npyx.spk_t.trn(dp,u)

    The units can also be accessed through the 'unit' attributes of the graph nodes:
    >>> merged.graph.nodes[u]['unit'].trn() returns the same thing as ds.units[u].trn()

    For multi probes recordings:
    >>> dp_dic = {'name_probe_1':'path/to/kilosort/output', ...
            }
    >>> merged = Merger(dp_dic)

    Here merged has the same structure as for a single path, but
    - nested into dictionaries for dataset-specific items: merged.dp becomes multimerged.dp['name_probe_1'], merged.units becomes multimerged.units['name_probe_1'] etc
    - still accessible directly for across-datasets items: merged.graph remains multimerged.graph

    --------- Use graph related utilities (experimental) ---------

    Connect the graph with putative monosynaptic connections
    >>> merged.connect_graph()

    Plot the graph
    >>> merged.plot_graph()

    Get N of putative connections of a given node spotted on the graph
    >>> merged.get_node_edges(node)

    Only keep a set of relevant nodes or edges and plot it again
    >>> merged.keep_edges(edges_list)
    >>> merged.keep_nodes_list(nodes_list)
    >>> merged.plot_graph()

    every graph operation of dataset merger can be performed on external networkx graphs
    provided with the argument 'src_graph'
    >>> g=merged.get_graph_copy(mergerGraph='undigraph')
    >>> merged.keep_nodes_list(nodes_list, src_graph=g) # g itself will be modified, not need to do g=...
    >>> merged.plot_graph(graph_src=g)

    '''


    def __init__(self, datapaths, sync_idx3A=2):
        # Handle datapaths format
        val_e=ValueError('''
        Datapath should be either an existing kilosort path:
        'path/to/kilosort/output1'
        or a dict of such paths:
        {'name_probe_1':'path/to/kilosort/output1', ...}''')
        if type(datapaths) is dict:
            for i, v in datapaths.items():
                datapaths[i]=Path(v)
                if not datapaths[i].exists():
                    raise val_e
        else:
            try:
                datapaths=Path(datapaths)
                assert datapaths.exists()
            except:
                raise val_e
            datapaths={'prb1':datapaths}

        # Make an alphabetically indexed table of all fed datasets (if several)
        self.ds_table=pd.DataFrame(columns=['dataset_name', 'dp', 'probe'])
        for i, (prb, dp) in enumerate(datapaths.items()):
            self.ds_table.loc[i, 'dataset_name']=op.basename(dp)
            self.ds_table.loc[i, 'dp']=dp
            self.ds_table.loc[i, 'probe']=prb
        self.ds_table.insert(0, 'dataset_i', self.ds_table['dataset_name'].argsort()) # sort table by dataset_name alphabetically
        self.ds_table.sort_values('dataset_i', axis=0, inplace=True)
        self.ds_table.set_index('dataset_i', inplace=True)

        # Instanciate datasets and define the merger path, where the 'merged' data will be saved,
        # together with the dataset table, holding information about the datasets (indices, names, source paths etc)
        ds_names=''
        predirname=op.dirname(self.ds_table['dp'][0])
        self.ds={}
        for ds_i in self.ds_table.index:
            dp, prb = self.ds_table.loc[ds_i, 'dp'], self.ds_table.loc[ds_i, 'probe']
            self.ds[ds_i]=Dataset(dp, prb, ds_i)
            ds_names+='_'+self.ds[ds_i].name
            if predirname!=op.dirname(dp):
                print('WARNING: all your datasets are not stored in the same pre-directory - {} is picked anyway.'.format(predirname))
        self.dp_merged = Path(predirname) / ('merged'+ds_names) #Path(predirname, 'merger'+ds_names)
        old_prophyler_path = Path(predirname) / ('prophyler'+ds_names)
        if old_prophyler_path.exists():
            # backward compatibility - rename path
            print("Old prophyler detected - renamed according to merger convention.")
            breakpoint()
            os.rename(str(old_prophyler_path), str(self.dp_merged))
        self.name=ds_names
        print('>>> Merged data (from {} dataset(s)) will be saved here: {}.'.format(len(self.ds_table.index), self.dp_merged))
        if not op.isdir(self.dp_merged):
            os.mkdir(self.dp_merged)
        else:
            ds_table=pd.read_csv(Path(self.dp_merged, 'datasets_table.csv'), index_col='dataset_i') #pd.read_csv(Path(self.dp_merged, 'datasets_table.csv'), index_col='dataset_i')
            for ds_i in ds_table.index:
                try:
                    assert self.ds_table.loc[ds_i, 'probe']==ds_table.loc[ds_i, 'probe']
                except:
                    print('''WARNING you ran dataset merger on these {} datasets in the past \
                              but used the probe names {} for datasets {} ({} currently)!! Using new probe names. \
                              If you wish to use the same probe names as before, re-instanciate merger with these.\
                              '''.format(len(self.ds_table.index), ds_table['probe'].tolist(), ds_table.index.tolist(), self.ds_table['probe'].tolist()))
            del ds_table
        self.ds_table.to_csv(Path(self.dp_merged, 'datasets_table.csv')) # self.ds_table.to_csv(Path(self.dp_merged, 'datasets_table.csv'))

        # Load and save units qualities
        # + check whether datasets have been re-spike sorted if not first instanciation
        re_spksorted=False
        qualities=pd.DataFrame(columns=['cluster_id', 'group'])
        for ds_i in self.ds_table.index:
            cl_grp = load_units_qualities(self.ds[ds_i].dp)
            cl_grp['cluster_id']=cl_grp['cluster_id']+1e-1*ds_i
            qualities=qualities.append(cl_grp, ignore_index=True)
        qualities_dp=Path(self.dp_merged, 'cluster_group.tsv')
        if op.exists(qualities_dp):
            qualities_old=pd.read_csv(qualities_dp, sep='	')
            # only consider re-spike sorted if cluster indices have been changed, do not if only qualities were changed (spike times are unimpacted by that)
            if not np.all(np.isin(qualities_old.loc[:, 'cluster_id'], qualities.loc[:, 'cluster_id'])):
                re_spksorted=True
                print('New spike-sorting detected.')
        qualities.to_csv(qualities_dp, sep='	', index=False)

        # Merge spike times (or copy them if only 1 dataset)
        # Only if merged_clusters_times does not exist already or does but re-spikesorting has been detected
        merge_fname_times='spike_times'
        merge_fname_clusters='spike_clusters'
        if (not op.exists(Path(self.dp_merged, merge_fname_times+'.npy'))) or re_spksorted:
            print(">>> Loading spike trains of {} datasets...".format(len(self.ds_table.index)))
            spike_times, spike_clusters, sync_signals = [], [], []
            for ds_i in self.ds_table.index:
                ds=self.ds[ds_i]
                spike_times.append(np.load(Path(ds.dp, 'spike_times.npy')).flatten())
                spike_clusters.append(np.load(Path(ds.dp, 'spike_clusters.npy')).flatten())
                if len(self.ds_table.index)>1: # sync signals only needed if alignment necessary
                    ons, offs = get_npix_sync(ds.dp, output_binary = False, sourcefile='ap', unit='samples')
                    syncchan=ask_syncchan(ons)
                    ds.sync_chan_ons=ons[syncchan]
                    sync_signals.append(ds.sync_chan_ons)
            NspikesTotal=0
            for i in range(len(spike_times)): NspikesTotal+=len(spike_times[i])

            # If several datasets are fed to the merger, align their spike times.
            # Merged dataset is saved as two arrays:
            # spike_times and spike_clusters, as in a regular dataset,
            # but where spike_clusters is a larger int (1 is unit 1 from dataset 0, 1,000,000,001 is unit 1 from dataset 1)
            if len(self.ds_table.index)>1:
                print(">>> Aligning spike trains of {} datasets...".format(len(self.ds_table.index)))
                spike_times = align_timeseries(spike_times, sync_signals, 30000)
            # Now save the new array
            merged_spike_clusters=npa(zeros=(NspikesTotal), dtype=np.float64)
            merged_spike_times=npa(zeros=(NspikesTotal), dtype=np.uint64)
            cum_Nspikes=0
            for ds_i in self.ds_table.index:
                Nspikes=len(spike_times[ds_i])
                merged_spike_clusters[cum_Nspikes:cum_Nspikes+Nspikes]=spike_clusters[ds_i]+1e-1*ds_i
                merged_spike_times[cum_Nspikes:cum_Nspikes+Nspikes]=spike_times[ds_i]
                cum_Nspikes+=Nspikes
            merged_spike_clusters=merged_spike_clusters[np.argsort(merged_spike_times)]
            merged_spike_times=np.sort(merged_spike_times)
            np.save(Path(self.dp_merged, merge_fname_clusters+'.npy'), merged_spike_clusters)
            np.save(Path(self.dp_merged, merge_fname_times+'.npy'), merged_spike_times)
            print(f'>>> {merge_fname_times} and {merge_fname_clusters} saved at {self.dp_merged}.')
            del spike_times, spike_clusters, sync_signals, merged_spike_clusters, merged_spike_times

            print(f'>>> {merge_fname_times} and {merge_fname_clusters} found at {self.dp_merged} and memory mapped at self.merged_spike_clusters and self.merged_spike_clusters.')
            self.merged_spike_clusters=np.memmap(Path(self.dp_merged, merge_fname_clusters+'.npy'))
            self.merged_spike_times=np.memmap(Path(self.dp_merged, merge_fname_times+'.npy'))

        # CREATE A KEYWORD IN CCG FUNCTION: MERGED_DATASET=TRUE OR FALSE - IF TRUE, HANDLES LOADED FILE DIFFERENTLY

        # Create a networkX graph whose nodes are Units()
        self.undigraph=nx.MultiGraph() # Undirected multigraph - directionality is given by uSrc and uTrg. Several peaks -> several edges -> multigraph.
        self.dpnet=Path(self.dp_merged, 'network')
        if not op.isdir(self.dpnet): os.mkdir(self.dpnet)

        self.peak_positions={}
        self.units={}
        for ds_i in self.ds_table.index:
            ds=self.ds[ds_i]
            ds.get_peak_positions()
            for u, pos in ds.peak_positions.items():
                self.peak_positions[u]=pos+npa([100,0])*ds_i # Every dataset is offset by 100um on x
            for u in ds.get_good_units():
                unit=Unit(ds, u, self.undigraph) # Units are added to the same graph when initialized, even from different source datasets
                self.units[unit.nodename]=unit


    def get_graph(self, mergerGraph='undigraph'):
        assert mergerGraph in ['undigraph', 'digraph']
        if mergerGraph=='undigraph':
            return self.undigraph
        elif mergerGraph=='digraph':
            if 'digraph' not in dir(self): self.make_directed_graph()
            return self.digraph
        else:
            print("WARNING graph should be either 'undigraphundigraph=nx.MultiGraph()' to pick self.undigraph or 'digraph' to pick self.digaph. Aborting.")
            return

    def get_graph_copy(self, mergerGraph='undigraph'):
        return self.get_graph(mergerGraph).copy()

    def connect_graph(self, corr_type='connections', metric='amp_z', cbin=0.5, cwin=100, p_th=0.02, n_consec_bins=3,
                      fract_baseline=4./5, W_sd=10, test='Poisson_Stark', again=False, againCCG=False, plotsfcm=False,
                      drop_seq=['sign', 'time', 'max_amplitude'], name=None, use_template_for_peakchan=True, subset_selection='all',
                      mergerGraph='undigraph', src_graph=None):

        g=self.get_graph(mergerGraph) if src_graph is None else src_graph
        if g is None: return

        g.remove_edges_from(list(g.edges)) # reset
        graphs=[f for f in os.listdir(self.dpnet) if 'graph' in f] # Load list of saved graphs

        if len(graphs)>0:
            while 1:
                load_choice=input("""Saved graphs found in {}:{}. \
                      Dial a filename index to load it, or <sfc> to build it from the significant functional correlations table:""".format(Path(self.dp_merged, 'graph'), ["{}:{}".format(gi, g) for gi, g in enumerate(graphs)]))
                try: # works if an int is inputed
                    load_choice=int(ale(load_choice))
                    g=self.import_graph(Path(self.dpnet, graphs[load_choice]))
                    print("Building Dataset.graph from file {}.".format(graphs[load_choice]))
                    if graphs[load_choice].split('.')[-1]!='gpickle':
                        print("WARNING loaded does not have gpickle format - 'unit' attribute of graph nodes are not saved in this file.")
                    return
                except: # must be a normal or empty string
                    if load_choice=='sfc':
                        break
                    elif op.isfile(Path(self.dpnet, load_choice)):
                        g=self.import_graph(Path(self.dpnet, load_choice))
                        print("Building Dataset.graph from file {}.".format(load_choice))
                        if load_choice.split('.')[-1]!='gpickle':
                            print("WARNING loaded does not have gpickle format - 'unit' attribute of graph nodes are not saved in this file.")
                        return
                    else:
                        print("Filename or 'sfc' misspelled. Try again.")

        print("""Building graph connections from significant functional correlations table
              with cbin={}, cwin={}, p_th={}, n_consec_bins={}, fract_baseline={}, W_sd={}, test={}.""".format(cbin, cwin, p_th, n_consec_bins, fract_baseline, W_sd, test))
        sfc, sfcm, peakChs, sigstack, sigustack = gen_sfc(self.dp_merged, corr_type, metric, cbin, cwin,
                                p_th, n_consec_bins, fract_baseline, W_sd, test,
                                again, againCCG, drop_seq,
                                pre_chanrange=None, post_chanrange=None, units=None, name=name,
                                use_template_for_peakchan=use_template_for_peakchan, subset_selection=subset_selection)
        self.sfc = sfc
        criteria={'test':test, 'cbin':cbin, 'cwin':cwin, 'p_th':p_th, 'n_consec_bins':n_consec_bins, 'fract_baseline':fract_baseline, 'W_sd':W_sd}
        g=self.map_sfc_on_g(g, self.sfc, criteria)
        self.make_directed_graph()
        if plotsfcm:
            npyx.plot.plot_sfcm(self.dp_merged, corr_type, metric,
                                    cbin, cwin, p_th, n_consec_bins, fract_baseline, W_sd, test,
                                    depth_ticks=True, regions={}, reg_colors={}, again=again, againCCG=againCCG, drop_seq=drop_seq)

    def map_sfc_on_g(self, g, sfc, criteria):
        for i in sfc.index:
            (u1,u2)=sfc.loc[i,'uSrc':'uTrg']
            f=sfc.loc[i,'l_ms':].values
            g.add_edge(u1, u2, uSrc=u1, uTrg=u2,
                       amp=f[2], t=f[3], sign=sign(f[2]), width=f[1]-f[0], label=0,
                       n_triplets=f[4], n_bincrossing=f[5], bin_heights=f[6], entropy=f[7],
                       criteria=criteria)
        return g

    def make_directed_graph(self, src_graph=None, only_main_edges=False):
        '''
        Should be called once the edges have been manually curated:
        - if several edges remain between pairs of nodes and only_main_edge is set to True, the one with the biggest standard deviation is kept
        - if the edge a->b has t<-t_asym ms: directed b->a, >t_asym ms: directed a->b, -t_asym ms<t<t_asym ms: a->b AND b->a (use uSrc and uTrg to figure out who's a and b)

        The directed graph is built on the basis of the undirected graph hence no amplitude criteria (except from the case where only_main_edge is True)is used to build it, only time asymmetry criteria.
        To modify the 'significance' criteria of modulated crosscorrelograms, use self.connect_graph() parameters.

        if t_asym is negative, peaks within the -t_asym, t_asym span will be bidirectionally drawn.
        '''
        g=self.get_graph('undigraph') if src_graph is None else src_graph
        if g is None: return



        # self.undigraph becomes a directed graph, its undirected version is saved as self.undigraph
        digraph=nx.MultiDiGraph()
        digraph.add_nodes_from(g.nodes(data=True))

        # - if several edges between pairs of nodes, keep the one with the biggest standard deviation
        # - if the edge a->b has t<-1ms: directed b->a, >1ms: directed a->b, -1ms<t<1ms: a->b AND b->a (use uSrc and uTrg to figure out who's a and b)

        if only_main_edges:
            self.keep_edges(mergerGraph='undigraph', src_graph=g, edges_types='main')
        for edge in self.get_edges(frmt='list', src_graph=g):
            uSrc=self.get_edge_attribute(edge, 'uSrc', mergerGraph='undigraph', src_graph=src_graph)
            uTrg=self.get_edge_attribute(edge, 'uTrg', mergerGraph='undigraph', src_graph=src_graph)
            t=self.get_edge_attribute(edge, 't', mergerGraph='undigraph', src_graph=src_graph)
            amp=self.get_edge_attribute(edge, 'amp', mergerGraph='undigraph', src_graph=src_graph)
            width=self.get_edge_attribute(edge, 'width', mergerGraph='undigraph', src_graph=src_graph)
            label=self.get_edge_attribute(edge, 'label', mergerGraph='undigraph', src_graph=src_graph)
            criteria=self.get_edge_attribute(edge, 'criteria', mergerGraph='undigraph', src_graph=src_graph)

            # Just make all the edges directed
            if t>0: # if close to 0 bidirectional, if >1
                digraph.add_edge(uSrc, uTrg, uSrc=uSrc, uTrg=uTrg,
                                                   amp=amp, t=t, sign=sign(amp), width=width, label=label,
                                                   criteria=criteria)
            if t<0:
                digraph.add_edge(uTrg, uSrc, uSrc=uSrc, uTrg=uTrg,
                                                   amp=amp, t=t, sign=sign(amp), width=width, label=label,
                                                   criteria=criteria)

        if src_graph is None:
            self.digraph=digraph.copy()
        else:
            return digraph


    def get_nodes(self, frmt='array', mergerGraph='undigraph', src_graph=None):
        g=self.get_graph(mergerGraph) if src_graph is None else src_graph
        if g is None: return

        assert frmt in ['list', 'array', 'dict', 'dataframe']

        if frmt=='dict':
            nodes={}
            for n, na in g.nodes(data=True):
                nodes[n]=na
            if frmt=='dataframe':
                nodes=pd.DataFrame(data=nodes) # multiIndexed dataframe where lines are attributes and collumns edges
                nodes=nodes.T
        else:
            nodes=list(g.nodes())
            if frmt=='array':
                nodes=npa(nodes)

        return nodes

    def get_edges(self, frmt='array', keys=True, mergerGraph='undigraph', src_graph=None):
        g=self.get_graph(mergerGraph) if src_graph is None else src_graph
        if g is None: return

        assert frmt in ['list', 'array', 'dict', 'dataframe']

        if frmt in ['dict', 'dataframe']:
            edges={}
            for e in g.edges(data=True, keys=keys):
                edges[e[0:-1]]=e[-1]
            if frmt=='dataframe':
                edges=pd.DataFrame(data=edges) # multiIndexed dataframe where lines are attributes and collumns edges
                edges=edges.T
                edges.index.names=['node1', 'node2', 'key']
        else:
            edges=list(g.edges(keys=keys))
            if frmt=='array':
                edges=npa(edges)
        return edges

    def get_node_attributes(self, n, mergerGraph='undigraph', src_graph=None):
        return self.get_nodes(frmt='dict', mergerGraph=mergerGraph, src_graph=src_graph)[n]

    def get_node_attribute(self, n, at, mergerGraph='undigraph', src_graph=None):
        assert at in ['unit', 'groundtruthCellType', 'classifiedCellType']
        return self.get_node_attributes(n, mergerGraph, src_graph)[at]

    def set_node_attribute(self, n, at, at_val, mergerGraph='undigraph', src_graph=None):
        g=self.get_graph(mergerGraph) if src_graph is None else src_graph
        if g is None: return

        assert at in ['unit', 'groundtruthCellType', 'classifiedCellType']
        nx.set_node_attributes(g, {n:{at:at_val}})

    def get_edge_keys(self, e, mergerGraph='undigraph', src_graph=None):
        assert len(e)==2
        npe=self.get_edges(mergerGraph=mergerGraph, src_graph=src_graph)
        keys=npe[((npe[:,0]==e[0])&(npe[:,1]==e[1]))|((npe[:,0]==e[1])&(npe[:,1]==e[0]))][:,2]
        return keys

    def get_edge_attributes(self, e, mergerGraph='undigraph', src_graph=None):

        assert len(e)==2 or len(e)==3
        e = tuple(e)
        if len(e)==3:
            try: # check that nodes are in the right order - multi directed graph
                return self.get_edges(frmt='dict', mergerGraph=mergerGraph, src_graph=src_graph)[e]
            except:
                return self.get_edges(frmt='dict', mergerGraph=mergerGraph, src_graph=src_graph)[(e[1],e[0],e[2])]
        elif len(e)==2:
            keys=self.get_edge_keys(e, mergerGraph=mergerGraph, src_graph=src_graph)
            edges={}
            for k in keys:
                try: # check that nodes are in the right order - multi directed graph
                    edges[k]=self.get_edges(frmt='dict', mergerGraph=mergerGraph, src_graph=src_graph)[(e[0],e[1],k)]
                except:
                    edges[k]=self.get_edges(frmt='dict', mergerGraph=mergerGraph, src_graph=src_graph)[(e[1],e[0],k)]
            return edges

    def get_edge_attribute(self, e, at, mergerGraph='undigraph', src_graph=None):

        assert at in ['uSrc','uTrg','amp','t','sign','width','label','criteria']
        return self.get_edge_attributes(e, mergerGraph, src_graph)[at]

    def set_edge_attribute(self, e, at, at_val, mergerGraph='undigraph', src_graph=None):
        g=self.get_graph(mergerGraph) if src_graph is None else src_graph
        if g is None: return

        assert at in ['uSrc','uTrg','amp','t','sign','width','label','criteria']
        assert len(e)==2 or len(e)==3
        e = tuple(e)
        nx.set_edge_attributes(g, {e:{at:at_val}})

    def get_edges_with_attribute(self, at, at_val, logical='==', tolist=True, mergerGraph='undigraph', src_graph=None):
        edges=self.get_edges(frmt='dataframe', keys=True, mergerGraph=mergerGraph, src_graph=src_graph) # raws are attributes, columns indices
        assert at in edges.index
        ops={'==':operator.eq, '!=':operator.ne, '<':operator.lt, '<=':operator.le, '>':operator.gt, '>=':operator.ge}
        assert logical in ops.keys()
        ewa=edges.columns[ops[logical](edges.loc[at,:], at_val)]
        return ewa.to_list() if tolist else ewa


    def get_node_edges(self, u, mergerGraph='undigraph', src_graph=None):
        g=self.get_graph(mergerGraph) if src_graph is None else src_graph
        if g is None: return
        node_edges={}
        for dsiunt, e_unt in g[u].items():
            dsi, unt = dsiunt.split('_')
            node_edges[unt]=[len(e_unt), '@{}'.format(self.ds[dsi].peak_channels[self.ds[dsi].peak_channels[:,0]==unt, 0])]

        return node_edges

    def keep_nodes_list(self, nodes_list, mergerGraph='undigraph', src_graph=None):
        '''
        Remove edges not in edges_list if provided.
        edges_list can be a list of [(u1, u2),] 2elements tuples or [(u1,u2,key),] 3 elements tuples.
        If 3 elements, the key is ignored and all edges between u1 and u2 already present in self.undigraph are kept.

        if src_graph is provided, operations are performed on it and the resulting graph is returned.
        else, nothing is returned since operations are performed on self attribute undigraph or digraph.
        '''
        g=self.get_graph(mergerGraph) if src_graph is None else src_graph
        if g is None: return

        if len(nodes_list)==0:
            return g
        npn=npa(self.get_nodes(mergerGraph=mergerGraph, src_graph=src_graph))
        nodes_list_idx=npa([])
        for n in nodes_list:
            try:
                #keys=self.get_edge_keys(e, mergerGraph=mergerGraph)
                #assert np.any((((npe[:,0]==e[0])&(npe[:,1]==e[1]))|((npe[:,0]==e[1])&(npe[:,1]==e[0]))))
                nodes_list_idx=np.append(nodes_list_idx, np.nonzero(npn==n)[0])
            except:
                print('WARNING edge {} does not exist in graph {}! Abort.'.format(n, g))
        nodes_list_idx=npa(nodes_list_idx, dtype=np.int64)
        nodes_to_remove=npn[~np.isin(np.arange(len(npn)),nodes_list_idx)]
        g.remove_nodes_from(nodes_to_remove)

        # if g is set as self.undigraph or self.digraph, they actually point to the same object and self attribute gets updated!
        # So no need to explicit 'if src_graph is None: self.undigraph=g' or whatever
        if src_graph is not None:
            return g

    def keep_edges(self, edges_list=None, edges_types=None, operator='and', mergerGraph='undigraph', src_graph=None, use_edge_key=True, t_asym=1):
        '''
        Remove edges not in edges_list if provided.
        edges_list can be a list of [(u1, u2),] 2elements tuples or [(u1,u2,key),] 3 elements tuples.
        If 3 elements, the key is ignored and all edges between u1 and u2 already present in self.undigraph are kept.

        if src_graph is provided, operations are performed on it and the resulting graph is returned.
        else, nothing is returned since operations are performed on self attribute undigraph or digraph.

        edges_type must be either in ['main', '-', '+', 'ci'] or a list of these # ci stands for common input.
        IF IT IS A LIST OF THESE, THE OR OPERATOR WILL BE USED BETWEEN THEM (['-', 'main'] will return some positive ccgs!)
        If edges_list is not None, the edges_list is kept and edges_type argument is ignored.
        '''
        g=self.get_graph(mergerGraph) if src_graph is None else src_graph
        if g is None: return
        if edges_list is None and edges_types is None:
            print('WARNING you should not call keep_edges() without providing any edges_list or edges_type to keep. Aborting.')
            return

        # Select edges to keep if necessary
        if edges_list is None:
            for et in edges_types: assert et in ['main', '-', '+', 'ci']
            assert type(edges_types) in [list, np.ndarray]
            # Use dfe to store edges that one wants to keep or not
            dfe=self.get_edges(frmt='dataframe', mergerGraph=mergerGraph, src_graph=g)
            npe=self.get_edges(mergerGraph=mergerGraph, src_graph=g)
            if not any(dfe):
                print('WARNING merger.keep_edges function called but the provided graph does not seem to have any edges to work on! Use connect_graph first. Aborting.')
                return g

            # get amplitudes and times
            amp=dfe.loc[:,'amp']
            t=dfe.loc[:, 't']
            # Initiate masks
            mask, main_mask, plus_mask, minus_mask, ci_mask = (amp!=amp), (amp!=amp), (amp!=amp), (amp!=amp), (amp!=amp) # empty
            if '+' in edges_types:
                plus_mask=(amp>0)&((t<-t_asym)|(t>t_asym))
            if '-' in edges_types:
                minus_mask=(amp<0)&((t<-t_asym)|(t>t_asym))
            if 'ci' in edges_types:
                ci_mask=(amp>0)&(t>-t_asym)&(t<t_asym)
            if 'main' in edges_types:
                singleedges_mask=(amp==amp) # initiate as all and remove multi edges in fro loop later
                # multiedges are rows where key is 1 (or more)- to get the list of edges with more than 1 edge, get unit couples with at least key=1
                keys=dfe.index.get_level_values('key')
                multiedges=npa(dfe.index[keys==1].tolist())
                if len(multiedges)>0:
                    multiedges[:,:2]=np.sort(multiedges[:,:2], axis=1) # order every pair in the same way so that unique can find duplicates
                    multiedges=np.unique(multiedges, axis=0)
                    for me in multiedges:
                        me_subedges_mask=(((npe[:,0]==me[0])&(npe[:,1]==me[1]))|((npe[:,0]==me[1])&(npe[:,1]==me[0]))) # find all subedges of each listed multiedge
                        singleedges_mask=((singleedges_mask)&(~me_subedges_mask)) # use me_multiedges_mask to negatively define single edges
                        main_submask=(dfe['amp'].abs()==dfe['amp'][me_subedges_mask].abs().max()) # main edge: edge with absolute max value
                        if not np.count_nonzero(main_submask)==1:
                            main_submask=main_submask&npa([el in dfe[me_subedges_mask].index.tolist() for el in dfe.index.tolist()]) # select subset of edges between same nodes (in the same ccg, handles cases where 2 different ccgs have the exact main peak value)
                            if not np.count_nonzero(main_submask)==1: # >1 peaks r troughs at exact same height in same ccg...
                                random_true_idx=np.random.choice(np.nonzero(main_submask)[0])
                                main_submask=(amp!=amp)# reset
                                main_submask[random_true_idx]=True # keep only one
                        main_mask=main_mask|main_submask
                # Also add back pairs of nodes which had only a single edge...
                main_mask=main_mask|singleedges_mask

            if operator=='or': # starts from empty and gets filled
                for m in [main_mask,plus_mask,minus_mask,ci_mask]:
                    mask=mask|m # neutral masks are empty masks
            elif  operator=='and': # starts from fuill and gets emptied
                mask=(mask==mask)
                for m in [main_mask,plus_mask,minus_mask,ci_mask]:
                    if not any(m): m=(m==m) # neutral masks are full masks
                    mask=mask&m

            edges_to_keep=dfe.index[mask].tolist()
            edges_to_drop=dfe.drop(edges_to_keep).index.tolist()
            dfe.drop(edges_to_drop, inplace=True)
            edges_list=dfe.index.tolist()

        if not any(edges_list):
            print('WARNING merger.keep_edges function called but resulted in all edges being kept!')
            return g

        if use_edge_key and len(edges_list[0])!=3:
            print('WARNING use_edge_key is set to True but edges of provided edges_list do not contain any key. Setting use_edge_key to False-> every edges between given pairs of nodes will be kept.')
            use_edge_key=False

        edges_list_idx=npa([])
        for e in edges_list:
            try:
                if use_edge_key:
                    if g.__class__ is nx.classes.multidigraph.MultiDiGraph:
                        edges_list_idx=np.append(edges_list_idx, np.nonzero(((npe[:,0]==e[0])&(npe[:,1]==e[1])&((npe[:,2]==e[2])|(npe[:,2]==str(e[2])))))[0])
                    elif g.__class__ is nx.classes.multigraph.MultiGraph:
                        edges_list_idx=np.append(edges_list_idx, np.nonzero((((npe[:,0]==e[0])&(npe[:,1]==e[1])&((npe[:,2]==e[2])|(npe[:,2]==str(e[2]))))|((npe[:,0]==e[1])&(npe[:,1]==e[0])&((npe[:,2]==e[2])|(npe[:,2]==str(e[2]))))))[0])
                else:
                    if g.__class__ is nx.classes.multidigraph.MultiDiGraph:
                        edges_list_idx=np.append(edges_list_idx, np.nonzero(((npe[:,0]==e[0])&(npe[:,1]==e[1])))[0])
                    elif g.__class__ is nx.classes.multigraph.MultiGraph:
                        edges_list_idx=np.append(edges_list_idx, np.nonzero((((npe[:,0]==e[0])&(npe[:,1]==e[1]))|((npe[:,0]==e[1])&(npe[:,1]==e[0]))))[0])
            except:
                print('WARNING edge {} does not exist in graph {}! Abort.'.format(e, g))
        edges_list_idx=npa(edges_list_idx, dtype=np.int64).flatten()
        edges_to_remove=npe[~np.isin(np.arange(len(npe)),edges_list_idx)]
        edges_to_remove=[(etr[0], etr[1], ale(str(etr[2]))) for etr in edges_to_remove] #handles case when key is string due to Utype of np array due to string type of units
        g.remove_edges_from(edges_to_remove)

        return g

    def label_nodes(self, mergerGraph='undigraph', src_graph=None):
        g=self.get_graph(mergerGraph) if src_graph is None else src_graph
        if g is None: return

        for node in g.nodes:
            label=''
            while label=='': # if enter is hit
                label=input("\n\n || Unit {}- label?(<n> to skip):".format(node))
                if label=='n':
                    label=0 # status quo
                    break

            if src_graph is not None:
                self.set_node_attribute(node, 'groundtruthCellType', label, mergerGraph=mergerGraph, src_graph=src_graph) # update graph
            else:
                self.set_node_attribute(node, 'groundtruthCellType', label, mergerGraph=mergerGraph, src_graph=src_graph) # update graph
            dsi, idx = node.split('_')
            self.units[dsi][idx].groundtruthCellType=label # Update class
            print("Label of node {} was set to {}.\n".format(node, label))

    def label_edges(self, mergerGraph='undigraph', src_graph=None):
        g=self.get_graph(mergerGraph) if src_graph is None else src_graph
        if g is None: return

        edges_types=['asym_inh', 'sym_inh', 'asym_exc', 'sym_exc', 'inh_exc', 'PC_CNC', 'CS_SS', 'oscill']

        if g.number_of_edges==0:
            print("""No edge detected - connect the graph first by calling
                  Dataset.connect_graph(cbin, cwin, threshold, n_consec_bins, rec_section, again)
                  You will be offered to load a pre-labelled graph if you ever saved one.""")
            return

        n_edges_init=g.number_of_edges()
        for ei, edge in enumerate(list(g.edges)):
            ea=self.get_edge_attributes(edge, mergerGraph=mergerGraph, src_graph=src_graph) # u1, u2, i unpacked

            ##TODO - plt ccg from shared directory
            npyx.plot.plot_ccg(self.dp_merged, [ea['uSrc'],ea['uTrg']], ea['criteria']['cbin'], ea['criteria']['cwin'])

            label=''
            while label=='': # if enter is hit
                print(" \n\n || Edge {}/{} ({} deleted so far)...".format(ei, n_edges_init, n_edges_init-g.number_of_edges()))
                print(" || {0}->{1} (multiedge {2}) sig. corr.: \x1b[1m\x1b[36m{3:.2f}\x1b[0msd high, \x1b[1m\x1b[36m{4:.2f}\x1b[0mms wide, @\x1b[1m\x1b[36m{5:.2f}\x1b[0mms".format(ea['uSrc'], ea['uTrg'], edge[2], ea['amp'], ea['width'], ea['t']))
                print(" || Total edges of source unit: {}".format(self.get_node_edges(ea['uSrc'], mergerGraph=mergerGraph)))
                label=input(" || Current label: {}. New label? ({},\n || <s> to skip, <del> to delete edge, <done> to exit):".format(self.get_edge_attribute(edge,'label'), ['<{}>:{}'.format(i,v) for i,v in enumerate(edges_types)]))
                try: # Will only work if integer is inputted
                    label=ale(label)
                    label=edges_types[label]
                    self.set_edge_attribute(edge, 'label', label, mergerGraph=mergerGraph, src_graph=src_graph) # if src_graph is None, nothing will be returned
                    print(" || Label of edge {} was set to {}.\n".format(edge, label))
                except:
                    if label=='del':
                        g.remove_edge(*edge)
                        print(" || Edge {} was deleted.".format(edge))
                        break
                    elif label=='s':
                        self.set_edge_attribute(edge, 'label', 0, mergerGraph=mergerGraph, src_graph=src_graph) # status quo
                        break
                    elif label=='':
                        print("Whoops, looks like you hit enter. You cannot leave unnamed edges. Try again.")
                    elif label=="done":
                        print(" || Done - exitting.")
                        break
                    else:
                        self.set_edge_attribute(edge, 'label', label, mergerGraph=mergerGraph, src_graph=src_graph)
                        print(" || Label of edge {} was set to {}.\n".format(edge, label))
                        break
            if label=="done":
                break

        while 1:
            save=input("\n\n || Do you wish to save your graph with labeled edges? <any>|<enter> to save it, else <n>:")
            if save=='n':
                print(" || Not saving. You can still save the graph later by running ds.export_graph(name, frmt) with frmt in ['edgelist', 'adjlist', 'gexf', 'gml'] (default gpickle).")
                break
            else:
                pass
            name=input(" || Saving graph with newly labelled edges as graph_<name>. Name (<t> for aaaa-mm-dd_hh:mm:ss format):")
            while 1:
                formats=['gpickle', 'gexf', 'edgelist', 'adjlist', 'gml']
                frmt=input(" || In which format? {}".format(['<{}>:{}'.format(i,v) for i,v in enumerate(formats)]))
                try: # Will only work if integer is inputted
                    frmt=ale(frmt)
                    frmt=formats[frmt]
                    break
                except:
                    print(" || Pick an integer between {} and {}!".format(0, len(formats)-1))
                    pass

            file='graph_{}.{}'.format(name, frmt)
            if op.isfile(Path(self.dpnet,file)):
                ow=input(" || Warning, name already taken - overwrite? <y>/<n>:")
                if ow=='y':
                    print(" || Overwriting graph {}.".format(file))
                    self.export_graph(name, frmt, ow=True, mergerGraph=mergerGraph, src_graph=src_graph) # 'graph_' is always appended at the beginning of the file names. It allows to spot presaved graphs.
                    break
                elif ow=='n':
                    print(' || Ok, pick another name.')
                    pass
            else:
                print(" || Saving graph {}.".format(file))
                self.export_graph(name, frmt, mergerGraph=mergerGraph, src_graph=src_graph) # 'graph_' is always appended at the beginning of the file names. It allows to spot presaved graphs.
                break

    ## TODO - remove self.peakchannels from drawing - handle datasets independently
    def plot_graph(self, keep_edges_types=None, keep_edges_types_sequentially=True, keep_edges_types_operator='or', edges_list=None, t_asym=1,
                   edge_labels=False, node_labels=True, draw_edges=1,
                   nodes_size=400, nodes_color='grey', nodes_outline_color='k',
                   edges_width=5, edge_vmin=-7, edge_vmax=7, arrowsize=25, arrowstyle='-|>',
                   ylim=[4000, 0], figsize=(9, 24), show_cmap=True,
                   _format='pdf', saveDir='~/Desktop', saveFig=False,
                   mergerGraph='undigraph', src_graph=None):
        '''
        Plotting parameters:
            - edge_labels: bool, display labels on edges with edge info (amplitude, time...)
            - node_labels: bool, display node labels (unit index, node attributes)
            - node_size: nodes size | Default 400
            - nodes_color: string, nodes color. | Default': 'grey'
              Can be a dictionnary of {groundtruthCellTypeCategory:string color} such as
              {'SSpk':(57./255,170./255,53./255), 'CSpk':(211./255,181./255,0./255), 'CNC':(185./255,163./255,203./255)}
            - nodes_outline_color: nodes outline color | Default': 'black'
            - edges_width: edges width | Default: 5
            - edge_vmin, edge_vmax: minimum and maximum value of edges colorbar (blue to red, centered on (edge_vmax-edge_vmin)/2)
            - arrowsize: size of arrow heads
            - arrowstyle: style of arrow heads (typically '-|'> or '-[')| Default '-|>'
            - ylim: limits of section of probe plotted, in um [bottom, top] | Default: [4000, 0]
            - figsize: (x, y) | Default: (6, 24)
            - show_cmap: whether to show colormap or not
            - _format: save format | Default: pdf
            - saveFig: boolean, whether to save figure or not at saveDir

        Edge selection parameters:
            - t_asym: definition of crosscorrelogram assymmetry, defining +/- versus common_input (ms) | Default 1
            - edges_list: Provide a list of edges (fully customizable). Can be used with self.get_edges_with_attribute(at, at_val). Default: None
            - keep_edges_type: list of edge types :
              - None: everyone (default)
              - '+': edges whose t is >1ms or <-1ms and sign is 1,
              - '-': edges whose t is >1ms or <-1ms and sign is -1,
              - 'ci': edges whose t is >1ms or <-1ms and sign is 1
              - 'main': edge with highest absolute amplitude
            - keep_edges_types_sequentially: will keep edge types sequentially (first keep '-', then amongst these keep the main for instance).
              If set to False, edges types are considered together and the keep_edges_types_operator can be used to set in which manner (e.g.'-'or '+'). | Default True
            - keep_edges_types_operator: 'and' or 'or'. E.g. allows to keep edges '-' AND 'main', or '-' OR '+'.
              Only applies when keep_edges_types_sequential is set to False (i.e. edge categories are considered together).
              Warning: order of list matters, if keep_edges_type_operator is 'and'!
              ['-', 'main'] will first filter out the negative edges, then take the one with the highest nabsolute amplitude.
              ['main', '-'] will first remove the non main edges (some will be negative) then get rid of the non negative ones. So some negative will be gotten rid of.

            edges_list has the priority over edges_types.

        Other parameters:
            - src_graph: graph to plot. | Default: None
            - mergerGraph: as usual, used when no src_graph is provided, pick between 'undigraph' or 'digraph' stored as a merger attribute.
        '''
        g=self.get_graph(mergerGraph) if src_graph is None else src_graph
        if g is None: return

        g_plt=g.copy() # careful, do not create an alias but a copy or the original graph itself will be altered!

        # Remove edges not in edges_list if provided.
        # edges_list can be a list of [(u1, u2),] 2elements tuples or [(u1,u2,key),] 3 elements tuples.
        # If 3 elements, the key is ignored and all edges between u1 and u2 already present in self.undigraph are kept.
        if edges_list is not None:
            self.keep_edges(edges_list=edges_list, src_graph=g_plt, use_edge_key=True, t_asym=t_asym)
        if keep_edges_types is not None:
            if type(keep_edges_types)!=list: keep_edges_types = list(keep_edges_types)
            for et in keep_edges_types:assert et in ['-', '+', 'ci', 'main']
            assert keep_edges_types_operator in ['and', 'or']
            if keep_edges_types_sequentially:
                for et in keep_edges_types:
                    g_plt=self.keep_edges(edges_types=[et], operator=keep_edges_types_operator, src_graph=g_plt, use_edge_key=True, t_asym=t_asym)
            else:
                g_plt=self.keep_edges(edges_types=keep_edges_types, operator=keep_edges_types_operator, src_graph=g_plt, use_edge_key=True, t_asym=t_asym)

        ew = [self.get_edge_attribute(e, 'amp', mergerGraph=mergerGraph, src_graph=src_graph) for e in g_plt.edges]
        e_labels={e[0:2]:str(np.round(self.get_edge_attribute(e, 'amp', mergerGraph=mergerGraph, src_graph=src_graph), 2))\
                  +'@'+str(np.round(self.get_edge_attribute(e, 't', mergerGraph=mergerGraph, src_graph=src_graph), 1))+'ms' for e in g_plt.edges}

        fig, ax = plt.subplots(figsize=figsize)
        if type(nodes_color) is dict:
            if not 'other' in nodes_color.keys():
                print("Dictionnary of colors was provided but no 'other' category was listed - using 'grey'")
                nodes_color['other']='grey'
        else:
            assert type(nodes_color) in [str, np.str_]
        if node_labels or (type(nodes_color) is dict):
            if node_labels:nlabs={}
            if type(nodes_color) is dict:nCols=[]
            for node in list(g_plt.nodes):
                pct=self.get_node_attribute(node, 'groundtruthCellType', mergerGraph=mergerGraph, src_graph=src_graph)
                if type(nodes_color) is dict:
                    if pct in nodes_color.keys():
                        nCols.append(nodes_color[pct])
                    else:
                        nCols.append(nodes_color['other'])
                if node_labels:
                    cct=self.get_node_attribute(node, 'classifiedCellType', mergerGraph=mergerGraph, src_graph=src_graph)
                    l="{}".format(node)
                    if pct!='':
                        l+="\nput:{}".format(pct)
                    if cct!='':
                        l+="\ncla:{}".format(cct)
                    nlabs[node]=l
            if type(nodes_color) is dict:nodes_color=nCols
            if node_labels: nx.draw_networkx_labels(g_plt,self.peak_positions, nlabs, font_weight='bold', font_color='#000000FF', font_size=8)
            #nx.draw_networkx(g, pos=peak_pos, node_color='#FFFFFF00', edge_color='white', alpha=1, with_labels=True, font_weight='bold', font_color='#000000FF', font_size=6)
        nx.draw_networkx_nodes(g_plt, pos=self.peak_positions, node_color=nodes_color, edgecolors=nodes_outline_color, linewidths=2, alpha=1, node_size=nodes_size)

        if draw_edges:
            edges_cmap=npyx.plot.get_cmap('RdBu_r')
            nx.draw_networkx_edges(g_plt, pos=self.peak_positions, edge_color=ew, width=edges_width, alpha=1,
                                   edge_cmap=edges_cmap, edge_vmin=edge_vmin, edge_vmax=edge_vmax, arrowsize=arrowsize, arrowstyle=arrowstyle)
            if show_cmap:
                sm = plt.cm.ScalarMappable(cmap=edges_cmap, norm=plt.Normalize(vmin = edge_vmin, vmax=edge_vmax))
                sm._A = []
                axins = inset_axes(ax,
                       width="8%",  # width = 5% of parent_bbox width
                       height="20%",  # height : 50%
                       loc='lower left',
                       bbox_to_anchor=(1.25, 0., 1, 1),
                       bbox_transform=ax.transAxes,
                       borderpad=0,
                       )
                fig.colorbar(sm, cax=axins)#, ticks=np.arange(edge_vmin, edge_vmax, 2))
                axins.set_ylabel("z-score", labelpad=10, rotation=270, fontsize=30, fontweight='bold')
                axins.set_yticklabels(npa(axins.get_yticks()).astype(int), fontsize=26, fontweight='bold')

        if draw_edges and edge_labels:
            nx.draw_networkx_edge_labels(g_plt, pos=self.peak_positions, edge_labels=e_labels,font_color='black', font_size=8, font_weight='bold')

        hfont = {'fontname':'Arial'}
        ax.set_ylabel('Depth (\u03BCm)', fontsize=40, fontweight='bold', **hfont)
        ax.set_xlabel('Lat. position (\u03BCm)', fontsize=40, fontweight='bold', **hfont)
        ax.set_ylim(ylim) # flips the plot upside down
        ax.set_xlim([0,70])
        ax.tick_params(axis='both', reset=True, labelsize=12, top=0)
        ax.set_yticklabels(npa(ax.get_yticks()).astype(int), fontsize=30, fontweight='bold', **hfont)
        ax.set_xticklabels(ax.get_xticks().astype(int), fontsize=30, fontweight='bold', **hfont)
        ax2 = ax.twinx()
        ax2.set_ylabel('Channel #', fontsize=40, fontweight='bold', rotation=270, va='bottom', **hfont)
        ax2.set_ylim(ylim)
        ax2.set_yticklabels([int(384-yt/10) for yt in ax.get_yticks()], fontsize=30, fontweight='bold', **hfont) # SIMPLY RERUN THIS IS YOU SCALE PLOT ON QT GUI

        fig.tight_layout(rect=[0, 0.03, 0.9, 0.95])
        try:
            criteria=self.get_edge_attribute(list(g_plt.edges)[0], 'criteria', mergerGraph=mergerGraph, src_graph=src_graph)
            ax.set_title("Dataset:{}\n Significance criteria:\n{}test: {}-{}-{}-{}-{}-{}.".format(self.name,
                         criteria['test'], criteria['cbin'], criteria['cwin'], criteria['p_th'], criteria['n_consec_bins'], criteria['fract_baseline'], criteria['W_sd']), fontsize=14, fontweight='bold')
        except:
            print('/nWARNING Graph not connected! Run self.connect_graph() to do so!/n')


        if saveFig:
            saveDir=op.expanduser(saveDir)
            if not os.path.isdir(saveDir): os.mkdir(saveDir)
            fig.savefig(saveDir+'/{}_graph_{}_{}-{}-{}-{}noCSnoEdges.{}'.format(self.name, keep_edges_types, *criteria.values(), _format), format=_format)

        return fig


    def export_graph(self, name='', frmt='gpickle', ow=False, mergerGraph='undigraph', src_graph=None):
        '''
        name: any srting. If 't': will be graph_aaaa-mm-dd_hh:mm:ss
        frmt: any in ['edgelist', 'adjlist', 'gexf', 'gml'] (default gpickle)'''
        g=self.get_graph(mergerGraph) if src_graph is None else src_graph
        if g is None: return

        assert frmt in ['edgelist', 'adjlist', 'gexf', 'gml', 'gpickle']
        file=Path(self.dpnet, mergerGraph+'_'+name+'_'+op.basename(self.dp_merged)+'.'+frmt)
        filePickle=Path(self.dpnet, mergerGraph+'_'+name+'_'+op.basename(self.dp_merged)+'.gpickle')

        if op.isfile(filePickle) and not ow:
            print("File name {} already taken. Pick another name or run the function with ow=True to overwrite file.")
            return

        nx_exp={'edgelist':nx.write_edgelist, 'adjlist':nx.write_adjlist,'gexf':nx.write_gexf, 'gml':nx.write_gml, 'gpickle':nx.write_gpickle}
        print(1, name)
        if name=='t':
            name=time.strftime("%Y-%m-%d_%H:%M:%S")
            print(2, name)
        nx_exp['gpickle'](g, filePickle) # Always export in edges list for internal compatibility

        if frmt!='gpickle':
            if frmt=='gml' or frmt=='gexf':
                print("GML or GEXF files can only process elements convertable into strings. Getting rid of nodes 'unit' attributes.")
                gc=g.copy()
                for n in g.nodes:
                    del gc.nodes[n]['unit']
                if frmt=='gml':
                    nx_exp[frmt](gc, file, stringizer=str) # np.int not handled... Need to explicitely tell it to use str() for non native python data types...
                elif frmt=='gexf':
                    nx_exp[frmt](gc, file)
            else:
                nx_exp[frmt](g, file)

    @staticmethod
    def import_graph(name):
        '''
        name: path to file to import, ending in '.edgelist', '.adjlist', '.gexf', '.gml' or '.gpickle'
        '''
        assert op.isfile(name)
        frmt=name.split('.')[-1]
        assert frmt in ['edgelist', 'adjlist', 'gexf', 'gml', 'gpickle']
        nx_exp={'edgelist':nx.read_edgelist, 'adjlist':nx.read_adjlist,'gexf':nx.read_gexf, 'gml':nx.read_gml, 'gpickle':nx.read_gpickle}

        return nx_exp[frmt](name)

# def map_sfc_on_graph(g, dp, cbin=0.5, cwin=100, p_th=0.02, n_consec_bins=3, sgn=0, fract_baseline=4./5, W_sd=10, test='Poisson_Stark',
#              metric='amp_z', again=False, againCCG=False, name=None, use_template_for_peakchan=True, subset_selection='all'):
#     # If called in the context of CircuitMerger, add the connection to the graph
#     sfc, sfcm, peakChs, sigstack, sigustack = gen_sfc(dp, cbin, cwin,
#                                         p_th, n_consec_bins, sgn, fract_baseline, W_sd, test,
#                                         metric, again, againCCG, drop_seq,
#                                         pre_chanrange=None, post_chanrange=None, units=None, name=name,
#                                         use_template_for_peakchan=use_template_for_peakchan, subset_selection=subset_selection)
#     if test=='Normal_Kopelowitz':
#         criteria={'cbin':cbin, 'cwin':cwin, 'p_th':p_th, 'n_consec_bins':n_consec_bins, 'fract_baseline':fract_baseline, }
#     elif test=='':
#         criteria={'cbin':cbin, 'cwin':cwin, 'p_th':p_th, 'n_consec_bins':n_consec_bins, 'W_sd':W_sd}
#     for i in sfc.index:
#         (u1,u2)=sfc.loc[i,'uSrc':'uTrg']
#         f=sfc.loc[i,'l_ms':].data
#         g.add_edge(u1, u2, uSrc=u1, uTrg=u2,
#                    amp=f[2], t=f[3], sign=sign(f[2]), width=f[1]-f[0], label=0,
#                    n_triplets=f[4], n_bincrossing=f[5], bin_heights=f[6], entropy=f[7],
#                    criteria=criteria)
#     return g

def ask_syncchan(ons):
    chan_len=''.join([f'chan {k} ({len(v)} events).\n' for k,v in ons.items()])
    syncchan=None
    while (syncchan is None):
        syncchan=input(f'Data found on sync channels:\n{chan_len}Which channel shall be used to synchronize probes? >>> ')
        try:
            syncchan=int(syncchan)
            if syncchan not in ons.keys():
                print(f'!! You need to feed an integer amongst {list(ons.keys())}!')
                syncchan=None
        except:
            print('!! You need to feed an integer!')
            syncchan=None
        if syncchan is not None:
            if not any(ons[syncchan]):
                print('!! This sync channel does not have any events! Pick another one!')
                syncchan=None
    return syncchan


class Dataset:

    def __repr__(self):
        return 'Neuropixels dataset at {}.'.format(self.dp)

    def __init__(self, datapath, probe_name='prb1', dataset_index=0, dataset_name=None):

        # Handle datapaths format
        try:
            datapath=Path(datapath)
            assert datapath.exists()
        except:
            raise ValueError('''datapath should be an existing kilosort path:
                    'path/to/kilosort/output1'.''')

        self.dp = Path(datapath)
        self.meta=read_spikeglx_meta(self.dp)
        self.probe_version=self.meta['probe_version']
        self.ds_i=dataset_index
        self.name=self.dp.name if dataset_name is None else dataset_name
        self.prb_name=probe_name
        self.params={}; params=imp.load_source('params', Path(self.dp,'params.py').absolute().as_posix())
        for p in dir(params):
            exec("if '__'not in '{}': self.params['{}']=params.{}".format(p, p, p))
        self.fs=self.meta['sRateHz']
        self.endTime=self.meta['fileTimeSecs']
        self.chan_map=chan_map(self.dp, y_orig='surface')
        sk_output_cm=chan_map(self.dp, y_orig='surface', probe_version='local')
        if not np.all(np.isin(sk_output_cm[:,0], self.chan_map[:,0])):
            if len(sk_output_cm[:,0])>len(self.chan_map[:,0]):
                print("WARNING looks like the channel map outputted by kilosort has more channels than expected ({})...".format(len(sk_output_cm[:,0])))
                self.chan_map=sk_output_cm
            else:
                raise "Local channel map comprises channels not found in expected channels given matafile probe type."

    def get_units(self):
        return npyx.gl.get_units(self.dp)

    def get_good_units(self):
        return npyx.gl.get_units(self.dp, quality='good')

    def get_peak_channels(self, use_template=True):
        self.peak_channels = get_depthSort_peakChans(self.dp, use_template=use_template)# {mainChans[i,0]:mainChans[i,1] for i in range(mainChans.shape[0])}
        return self.peak_channels

    def get_peak_positions(self, use_template=True):
        self.get_peak_channels(use_template)
        peak_pos=npa(zeros=(self.peak_channels.shape[0], 3), dtype=np.int64)
        peak_pos[:,0]=self.peak_channels[:,0] # units
        pos_idx=[] # how to get rid of for loop??
        for ch in self.peak_channels[:,1]:
            pos_idx.append(np.nonzero(np.isin(self.chan_map[:,0], ch))[0][0])
        peak_pos[:,1:]=self.chan_map[:,1:][pos_idx, :]
        self.peak_positions_real=peak_pos

        # Homogeneously distributes neurons on same channels around mother channel to prevent overlap
        for pp in np.unique(peak_pos[:,1:], axis=0): # space positions if several units per channel
            boolarr=(pp[0]==peak_pos[:,1])&(pp[1]==peak_pos[:,2])
            n1=sum(x > 0 for x in boolarr)
            if n1>1:
                for i in range(n1):
                    x_spacing=16# x spacing is 32um
                    spacing=x_spacing*1./(n1+1)
                    boolidx=np.nonzero(boolarr)[0][i]
                    peak_pos[boolidx,1]=peak_pos[boolidx,1]-x_spacing*1./2+(i+1)*spacing # 1 is index of x value

        self.peak_positions={int(pp[0]):pp[1:] for pp in peak_pos}
        return self.peak_positions

class Unit:
    '''The object Unit does not store anything itself except from its dataset and index.
       It is simply a convenient OOP wrapper to compute/load its attributes (crosscorrelogram. waveform....
    '''

    def __repr__(self):
        return 'Unit {} from dataset {}.'.format(self.idx, self.ds.name)

    def __init__(self, dataset, index, graph):
        self.ds=dataset
        self.dp = self.ds.dp
        self.idx=float(index)
        self.putativeCellType='' # Set by the experimentalist manually
        self.groundtruthCellType='' # Either opto or because of cs/ss pause motif
        self.classifiedCellType='' # Output of the classifier
        self.undigraph = graph
        self.get_peak_position()
        # self refers to the instance not the class, hehe
        self.nodename=float(index)

        self.undigraph.add_node(self.nodename, unit=self, X=self.peak_position_real[0], Y=self.peak_position_real[1], posReal=self.peak_position_real, putativeCellType=self.putativeCellType, groundtruthCellType=self.groundtruthCellType, classifiedCellType=self.classifiedCellType)

    def get_peak_channel(self):
        self.peak_channel=get_peak_chan(self.dp, self.idx)

    def get_peak_position(self):
        self.get_peak_channel()

        # Get peak channel xy positions
        self.peak_position_real=self.ds.peak_positions_real[self.idx==self.ds.peak_positions_real[:,0], 1:].flatten()

    def trn(self, rec_section='all'):
        return npyx.spk_t.trn(self.dp, self.idx, subset_selection=rec_section)

    def trnb(self, bin_size, rec_section='all'):
        return npyx.spk_t.trnb(self.dp, self.idx, bin_size, subset_selection=rec_section)

    def ids(self):
        return npyx.spk_t.ids(self.dp, self.idx)

    def isi(self, rec_section='all'):
        return npyx.spk_t.isi(self.dp, self.idx, subset_selection=rec_section)

    def acg(self, cbin, cwin, normalize='Hertz', subset_selection='all'):
        return npyx.corr.acg(self.dp, self.idx, bin_size=cbin, win_size=cwin, normalize=normalize, subset_selection=subset_selection)

    def ccg(self, U, cbin, cwin, fs=30000, normalize='Hertz', ret=True, sav=True, prnt=True, rec_section='all', again=False):
        return npyx.corr.ccg(self.dp, [self.idx]+list(U), cbin, cwin, fs, normalize, ret, sav, prnt, rec_section, again)

    def wvf(self, n_waveforms=100, t_waveforms=82, wvf_subset_selection='regular', wvf_batch_size=10):
        return npyx.spk_wvf.wvf(self.dp, self.idx, n_waveforms, t_waveforms, wvf_subset_selection, wvf_batch_size, True, True)

    def plot_acg(self, cbin=0.2, cwin=80, normalize='Hertz', color=0, saveDir='~/Downloads', saveFig=True, prnt=False, show=True,
             pdf=True, png=False, rec_section='all', labels=True, title=None, ref_per=True, saveData=False, ylim=0):

        npyx.plot.plot_acg(self.dp, self.idx, cbin, cwin, normalize, color, saveDir, saveFig, prnt, show,
             pdf, png, rec_section, labels, title, ref_per, saveData, ylim)

    def plot_ccg(self, units, cbin=0.2, cwin=80, normalize='Hertz', saveDir='~/Downloads', saveFig=False, prnt=False, show=True,
             pdf=False, png=False, rec_section='all', labels=True, std_lines=True, title=None, color=-1, CCG=None, saveData=False, ylim=0):

        npyx.plot.plot_ccg(self.dp, [self.idx]+list(units), cbin, cwin, normalize, saveDir, saveFig, prnt, show,
                 pdf, png, rec_section, labels, std_lines, title, color, CCG, saveData, ylim)

    def connections(self):
        return dict(self.undigraph[self.idx])



    # a=npa([ 0,  1,  2,  3,  9, 13, 16, 29])
    # aa=npa([ 0,  2, 13])

    # b=npa([ 3,  4,  5,  7, 13, 17, 21, 34]) # should be exactly a, but 2-shifted + 1 of drift after 6, 2 of drift after 18
    # bb=npa([3, 6, 18])
