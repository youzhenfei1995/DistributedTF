"""
Executes synchronous population-based training on MNIST ConvNets using MPI for
Python, reporting its results at the end.

This file must be run with the following command:

mpiexec -n <m> python </path/to/>mnist_pbt_sync.py

where <m>, an integer no less than 2, is the number of processes to use.
"""

from typing import Union, Any, List, Tuple, Dict
from enum import Enum
from collections import OrderedDict
import math
from mpi4py import MPI
import datetime
import tensorflow as tf
from mnist_dataset import train, test
from mnist import set_mnist_data
from mnist_pbt import ConvNet


Device = Union[str, None]


class Attribute(Enum):
    """
    An attribute of a ConvNet that its Cluster can access remotely.
    """
    STEP_NUM = 0
    VALUE = 1
    UPDATE_HISTORY = 2
    ACCURACY = 3


GETTERS = {Attribute.STEP_NUM: (lambda graph: graph.step_num),
           Attribute.VALUE: (lambda graph: graph.get_value()),
           Attribute.UPDATE_HISTORY: (lambda graph: graph.get_update_history()),
           Attribute.ACCURACY: (lambda graph: graph.get_accuracy())
           }


class Instruction(Enum):
    """
    A type of instruction that a Cluster can send to its worker processes.
    """
    EXIT = 0
    INIT = 1
    GET = 2
    COPY_TRAIN_GET = 3


def worker(comm, cluster_rank):
    """
    The behavior of a Cluster's worker process.

    <comm> is the MPI Comm that the Cluster and its workers use to communicate,
    and <cluster_rank> is the rank of the Cluster's process.
    """
    config = tf.ConfigProto()
    config.gpu_options.per_process_gpu_memory_fraction = 0.1
    sess = tf.Session(config=config)
    device, start_num, end_num = comm.recv(source=cluster_rank)
    with tf.device(device):
        graphs = OrderedDict()
        for num in range(start_num, end_num):
            graphs[num] = ConvNet(num, sess)
        while True:
            data = comm.recv(source=cluster_rank)
            instruction = data[0]
            if instruction == Instruction.EXIT:
                break
            elif instruction == Instruction.INIT:
                for graph in graphs.values():
                    graph.initialize_variables()
            else:
                if instruction == Instruction.COPY_TRAIN_GET:
                    new_values = data[3]
                    for num, new_value in new_values.items():
                        graphs[num].set_value(new_value)
                        graphs[num].explore()
                    for graph in graphs.values():
                        graph.train()
                nums = data[1]
                attributes = data[2]
                attribute_getters = [GETTERS[attribute] for attribute in attributes]
                comm.send({num: tuple(getter(graphs[num]) for getter in attribute_getters) for num in nums},
                          dest=cluster_rank)


class Cluster():
    """
    A PBT Cluster that synchronously trains ConvNets, distributed over multiple
    worker processes, using MPI for Python.

    A Cluster's get_population() and get_highest_metric_graph() methods return
    copies of its ConvNets on the Cluster's own process.
    """
    def __init__(self, pop_size, comm, rank_devices):
        """
        Creates a new Cluster with <pop_size> ConvNets.

        <comm> is the MPI Comm that this Cluster and its worker processes use
        to communicate. <rank_devices> is a dictionary in which each key is a
        worker's process rank and its corresponding value is the TensorFlow
        device on which that worker should create its assigned ConvNets.

        worker(<comm>, <rank>), where <rank> is the rank of this Cluster's
        process, must be called independently in each worker process.
        """
        config = tf.ConfigProto()
        config.gpu_options.per_process_gpu_memory_fraction = 0.1
        self.sess = tf.Session(config=config)
        self.pop_size = pop_size
        self.comm = comm
        self.rank_graphs = {rank: [] for rank in rank_devices.keys()}
        self.graph_ranks = []
        graphs_per_worker = pop_size / len(rank_devices)
        graph_num = 0
        graphs_to_make = 0
        reqs = []
        for rank, device in rank_devices.items():
            graphs_to_make += graphs_per_worker
            start_num = int(graph_num)
            graph_num = int(min(graph_num + math.ceil(graphs_to_make), pop_size))
            self.rank_graphs[rank].extend(range(start_num, graph_num))
            self.graph_ranks.extend(rank for _ in range(start_num, graph_num))
            reqs.append(comm.isend((device, start_num, graph_num), dest=rank))
            graphs_to_make -= (graph_num - start_num)
        for req in reqs:
            req.wait()

    def initialize_variables(self):
        reqs = []
        for rank in self.rank_graphs.keys():
            reqs.append(self.comm.isend((Instruction.INIT,), dest=rank))
        for req in reqs:
            req.wait()
        print('Variables initialized')

    def get_population(self):
        attributes = self.get_attributes([Attribute.VALUE])
        population = []
        for num in range(self.pop_size):
            graph = ConvNet(num, self.sess)
            graph.set_value(attributes[num][0])
            population.append(graph)
        return population

    def get_highest_metric_graph(self):
        attributes = self.get_attributes([Attribute.ACCURACY])
        best_num = None
        best_acc = None
        for num in range(self.pop_size):
            accuracy = attributes[num][0]
            if best_num is None or accuracy > best_acc:
                best_num = num
                best_acc = accuracy
        graph = ConvNet(best_num, self.sess)
        best_rank = self.graph_ranks[best_num]
        self.comm.send((Instruction.GET, [best_num], [Attribute.VALUE]), dest=best_rank)
        graph.set_value(self.comm.recv(source=best_rank)[best_num][0])
        return graph

    def _exploit_and_or_explore(self, attributes):
        for num in range(self.pop_size):
            print('Graph', num, 'accuracy:', attributes[num][1])
        new_values = {}
        if self.pop_size > 1:
            # Rank population by accuracy
            ranked_nums = sorted(range(self.pop_size), key=lambda num: attributes[num][1])
            # Bottom 20% copies top 20%
            worst_nums = ranked_nums[:math.ceil(0.2 * len(ranked_nums))]
            best_nums = ranked_nums[math.floor(0.8 * len(ranked_nums)):]
            best_attributes = self.get_attributes([Attribute.VALUE], best_nums)
            for i in range(len(worst_nums)):
                print('Graph', worst_nums[i], 'copying graph', best_nums[i])
                new_values[worst_nums[i]] = best_attributes[i][0]
        return new_values

    def train(self, until_step_num):
        attribute_ids = [Attribute.STEP_NUM, Attribute.ACCURACY]
        attributes = self.get_attributes(attribute_ids)
        while True:
            keep_training = False
            new_values = {}
            for graph_attributes in attributes:
                step_num = graph_attributes[0]
                if step_num < until_step_num:
                    keep_training = True
                    if step_num > 0:
                        print('Exploiting/exploring')
                        new_values = self._exploit_and_or_explore(attributes)
                        print('Finished exploiting/exploring')
                        break
            if keep_training:
                print('Starting training runs')
                attributes_dict = {}
                reqs = []
                for rank, graphs in self.rank_graphs.items():
                    rank_new_values = {num: new_values[num] for num in graphs if num in new_values.keys()}
                    reqs.append(self.comm.isend(
                        (Instruction.COPY_TRAIN_GET, graphs, attribute_ids, rank_new_values), dest=rank))
                for req in reqs:
                    req.wait()
                for rank in self.rank_graphs.keys():
                    attributes_dict.update(self.comm.recv(source=rank))
                attributes = [attributes_dict[num] for num in range(self.pop_size)]
                print('Finished training runs')
            else:
                break

    def get_attributes(self, attribute_ids, graph_nums=None):
        """
        Returns the attributes specified by <attribute_ids> of this Cluster's
        ConvNets with numbers <graph_nums>.

        The return value will be a list of tuples, each containing the
        attributes of one ConvNet in the order they are listed in
        <attribute_ids>. If <graph_nums> is None or not specified, the list
        will contain a tuple for each of this Cluster's ConvNets in order of
        increasing number. Otherwise, the list will contain a tuple for each
        ConvNet in the order their numbers appear in <graph_nums>.
        """
        if graph_nums is None:
            graph_nums = list(range(self.pop_size))
            rank_graphs = self.rank_graphs
        else:
            rank_graphs = {}
            for num in graph_nums:
                rank = self.graph_ranks[num]
                if rank in rank_graphs:
                    rank_graphs[rank].append(num)
                else:
                    rank_graphs[rank] = [num]
        attributes_dict = {}
        reqs = []
        for rank, graphs in rank_graphs.items():
            reqs.append(self.comm.isend((Instruction.GET, graphs, attribute_ids), dest=rank))
        for req in reqs:
            req.wait()
        for rank in rank_graphs.keys():
            data = self.comm.recv(source=rank)
            print(data)
            attributes_dict.update(data)
        return [attributes_dict[num] for num in graph_nums]

    def exit_workers(self):
        """
        Instructs this Cluster's worker processes to exit their worker()
        functions, rendering this Cluster unable to communicate with them.

        None of this Cluster's methods should be called after this one.
        """
        reqs = []
        for rank in self.rank_graphs.keys():
            reqs.append(self.comm.isend((Instruction.EXIT,), dest=rank))
        for req in reqs:
            req.wait()


set_mnist_data(train('MNIST_data/'), test('MNIST_data/'))
comm = MPI.COMM_WORLD
if comm.Get_rank() == 0:
    print('Master starting')
    cluster = Cluster(3, comm, {rank: '/cpu:0' for rank in range(1, comm.Get_size())})
    cluster.initialize_variables()
    training_start = datetime.datetime.now()
    cluster.train(20)
    print('Training time:', datetime.datetime.now() - training_start)
    attributes = cluster.get_attributes(
        [Attribute.STEP_NUM, Attribute.UPDATE_HISTORY, Attribute.ACCURACY])
    ranked_nums = sorted(range(len(attributes)), key=lambda num: -attributes[num][2])
    print()
    for num in ranked_nums:
        graph_info = attributes[num]
        print('Graph', num)
        print('Accuracy:', graph_info[2])
        print('Hyperparameter update history:')
        print()
        print(''.join(str(update) for update in graph_info[1]))
    cluster.exit_workers()
else:
    print('Worker {} starting'.format(comm.Get_rank()))
    worker(comm, 0)