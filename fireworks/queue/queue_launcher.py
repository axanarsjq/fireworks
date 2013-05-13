#!/usr/bin/env python

"""
This module is used to submit jobs to a queue on a cluster. It can submit a single job, \
or if used in "rapid-fire" mode, can submit multiple jobs within a directory structure. \
The details of job submission and queue communication are handled using Queueadapter, \
which specifies a QueueAdapter as well as desired properties of the submit script.
"""

import os
import glob
import string
import time
from fireworks.core.fworker import FWorker
from fireworks.utilities.fw_serializers import load_object
from fireworks.utilities.fw_utilities import get_fw_logger, log_exception, create_datestamp_dir, get_slug
from fireworks.core.fw_config import FWConfig

__author__ = 'Anubhav Jain, Michael Kocher'
__copyright__ = 'Copyright 2012, The Materials Project'
__version__ = '0.1'
__maintainer__ = 'Anubhav Jain'
__email__ = 'ajain@lbl.gov'
__date__ = 'Dec 12, 2012'

# TODO: clean up method signatures

def launch_rocket_to_queue(launchpad, fworker, qadapter, launcher_dir='.', reserve=False, strm_lvl='INFO'):
    """
    Submit a single job to the queue.
    
    :param launchpad: (LaunchPad)
    :param fworker: (FWorker)
    :param qadapter: (QueueAdapterBase)
    :param launcher_dir: (str) The directory where to submit the job
    :param reserve: (bool) Whether to queue in reservation mode
    :param strm_lvl: (str) level at which to stream log messages
    """

    fworker = fworker if fworker else FWorker()
    launcher_dir = os.path.abspath(launcher_dir)
    l_logger = get_fw_logger('queue.launcher', l_dir=launchpad.logdir, stream_level=strm_lvl)
    qadapter = load_object(qadapter.to_dict())  # make a defensive copy, mainly for reservation mode

    # make sure launch_dir exists:
    if not os.path.exists(launcher_dir):
        raise ValueError('Desired launch directory {} does not exist!'.format(launcher_dir))

    if launchpad.run_exists(fworker):
        try:
            # get the queue adapter
            l_logger.debug('getting queue adapter')

            # move to the launch directory
            l_logger.info('moving to launch_dir {}'.format(launcher_dir))
            os.chdir(launcher_dir)

            if reserve:
                l_logger.debug('finding a FW to reserve...')
                fw, launch_id = launchpad._reserve_fw(fworker, launcher_dir)
                l_logger.info('reserved FW with fw_id: {}'.format(fw.fw_id))

                # set job name to the FW name
                job_name = get_slug(fw.name)
                job_name = job_name[0:20] if len(job_name)>20 else job_name
                qadapter.update({'job_name': job_name})  # set the job name to FW name

                if '_queueadapter' in fw.spec:
                    l_logger.debug('updating queue params using FireWork spec..')
                    qadapter.update(fw.spec['_queueadapter'])

                # update the exe to include the FW_id
                if 'singleshot' not in qadapter.get('rocket_launch', ''):
                    raise ValueError('Reservation mode of queue launcher only works for singleshot Rocket Launcher!')
                qadapter['rocket_launch'] += ' --fw_id {}'.format(fw.fw_id)

            # write and submit the queue script using the queue adapter
            l_logger.debug('writing queue script')
            with open(FWConfig().SUBMIT_SCRIPT_NAME, 'w') as f:
                queue_script = qadapter.get_script_str(launcher_dir)
                f.write(queue_script)
            l_logger.info('submitting queue script')
            reservation_id = qadapter.submit_to_queue(FWConfig().SUBMIT_SCRIPT_NAME)
            if not reservation_id:
                raise RuntimeError('queue script could not be submitted, check queue adapter and queue server status!')
            elif reserve:
                launchpad._set_reservation_id(launch_id, reservation_id)

        except:
            log_exception(l_logger, 'Error writing/submitting queue script!')
    else:
        l_logger.info('No jobs exist in the LaunchPad for submission to queue!')


def rapidfire(launchpad, fworker, qadapter, launch_dir='.', nlaunches=0, njobs_queue=10, njobs_block=500,
              sleep_time=None, reserve=False, strm_lvl='INFO'):
    """
    Submit many jobs to the queue.
    
    :param launchpad: (LaunchPad)
    :param fworker: (FWorker)
    :param qadapter: (QueueAdapterBase)
    :param launch_dir: directory where we want to write the blocks
    :param nlaunches: total number of launches desired
    :param njobs_queue: stops submitting jobs when njobs_queue jobs are in the queue
    :param njobs_block: automatically write a new block when njobs_block jobs are in a single block
    :param sleep_time: (int) secs to sleep between rapidfire loop iterations
    :param reserve: (bool) Whether to queue in reservation mode
    :param strm_lvl: (str) level at which to stream log messages
    """

    sleep_time = sleep_time if sleep_time else FWConfig().RAPIDFIRE_SLEEP_SECS
    launch_dir = os.path.abspath(launch_dir)
    nlaunches = -1 if nlaunches == 'infinite' else int(nlaunches)
    l_logger = get_fw_logger('queue.launcher', l_dir=launchpad.logdir, stream_level=strm_lvl)

    # make sure launch_dir exists:
    if not os.path.exists(launch_dir):
        raise ValueError('Desired launch directory {} does not exist!'.format(launch_dir))

    num_launched = 0
    try:
        l_logger.info('getting queue adapter')

        block_dir = create_datestamp_dir(launch_dir, l_logger)

        while True:
            # get number of jobs in queue
            jobs_in_queue = _get_number_of_jobs_in_queue(qadapter, njobs_queue, l_logger)

            while jobs_in_queue < njobs_queue and launchpad.run_exists(fworker):
                l_logger.info('Launching a rocket!')

                # switch to new block dir if it got too big
                if _njobs_in_dir(block_dir) >= njobs_block:
                    l_logger.info('Block got bigger than {} jobs.'.format(njobs_block))
                    block_dir = create_datestamp_dir(launch_dir, l_logger)

                # create launcher_dir
                launcher_dir = create_datestamp_dir(block_dir, l_logger, prefix='launcher_')
                # launch a single job
                launch_rocket_to_queue(launchpad, fworker, qadapter, launcher_dir, reserve, strm_lvl)
                num_launched += 1
                if num_launched == nlaunches:
                    break
                # wait for the queue system to update
                l_logger.info('Sleeping for {} seconds...zzz...'.format(FWConfig().QUEUE_UPDATE_INTERVAL))
                time.sleep(FWConfig().QUEUE_UPDATE_INTERVAL)
                jobs_in_queue = _get_number_of_jobs_in_queue(qadapter, njobs_queue, l_logger)

            if num_launched == nlaunches or nlaunches == 0:
                break
            l_logger.info('Finished a round of launches, sleeping for {} secs'.format(sleep_time))
            time.sleep(sleep_time)
            l_logger.info('Checking for Rockets to run...'.format(sleep_time))

    except:
        log_exception(l_logger, 'Error with queue launcher rapid fire!')


def _njobs_in_dir(block_dir):
    """
    Internal method to count the number of jobs inside a block

    :param block_dir: (str) the block directory we want to count the jobs in
    """
    return len(glob.glob('%s/launcher_*' % os.path.abspath(block_dir)))


def _get_number_of_jobs_in_queue(qadapter, njobs_queue, l_logger):
    """
    Internal method to get the number of jobs in the queue using the given job params. \
    In case of failure, automatically retries at certain intervals...
    
    :param qadapter: (QueueAdapter)
    :param njobs_queue: (int) The desired maximum number of jobs in the queue
    :param l_logger: (logger) A logger to put errors/info/warnings/etc.
    """

    RETRY_INTERVAL = 30  # initial retry in 30 sec upon failure

    jobs_in_queue = qadapter.get_njobs_in_queue()
    for i in range(FWConfig().QUEUE_RETRY_ATTEMPTS):
        if jobs_in_queue is not None:
            l_logger.info('{} jobs in queue. Maximum allowed by user: {}'.format(jobs_in_queue, njobs_queue))
            return jobs_in_queue
        l_logger.warn('Could not get number of jobs in queue! Sleeping {} secs...zzz...'.format(RETRY_INTERVAL))
        time.sleep(RETRY_INTERVAL)
        RETRY_INTERVAL *= 2
        jobs_in_queue = qadapter.get_njobs_in_queue()

    raise RuntimeError('Unable to determine number of jobs in queue, check queue adapter and queue server status!')
