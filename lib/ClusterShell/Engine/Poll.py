#
# Copyright CEA/DAM/DIF (2007, 2008, 2009)
#  Contributor: Stephane THIELL <stephane.thiell@cea.fr>
#
# This file is part of the ClusterShell library.
#
# This software is governed by the CeCILL-C license under French law and
# abiding by the rules of distribution of free software.  You can  use,
# modify and/ or redistribute the software under the terms of the CeCILL-C
# license as circulated by CEA, CNRS and INRIA at the following URL
# "http://www.cecill.info".
#
# As a counterpart to the access to the source code and  rights to copy,
# modify and redistribute granted by the license, users are provided only
# with a limited warranty  and the software's author,  the holder of the
# economic rights,  and the successive licensors  have only  limited
# liability.
#
# In this respect, the user's attention is drawn to the risks associated
# with loading,  using,  modifying and/or developing or reproducing the
# software by the user in light of its specific status of free software,
# that may mean  that it is complicated to manipulate,  and  that  also
# therefore means  that it is reserved for developers  and  experienced
# professionals having in-depth computer knowledge. Users are therefore
# encouraged to load and test the software's suitability as regards their
# requirements in conditions enabling the security of their systems and/or
# data to be ensured and,  more generally, to use and operate it in the
# same conditions as regards security.
#
# The fact that you are presently reading this means that you have had
# knowledge of the CeCILL-C license and that you accept its terms.
#
# $Id$

"""
A poll() based ClusterShell Engine.

The poll() system call is available on Linux and BSD.
"""

from Engine import *

from ClusterShell.Worker.EngineClient import EngineClientEOF

import errno
import os
import select
import signal
import sys
import time
import thread


class EnginePoll(Engine):
    """
    Poll Engine

    ClusterShell engine using the select.poll mechanism (Linux poll()
    syscall).
    """
    def __init__(self, info):
        """
        Initialize Engine.
        """
        Engine.__init__(self, info)
        try:
            # get a polling object
            self.polling = select.poll()
        except AttributeError:
            print >> sys.stderr, "Error: select.poll() not supported"
            raise

        # runloop-has-exited flag
        self.exited = False

    def _modify_specific(self, fd, event, setvalue):
        """
        Engine-specific modifications after a interesting event change for
        a file descriptor. Called automatically by Engine register/unregister and
        set_events().  For the poll() engine, it reg/unreg or modifies the event mask
        associated to a file descriptor.
        """
        self._debug("MODSPEC fd=%d event=%x setvalue=%d" % (fd, event, setvalue))

        if setvalue:
            eventmask = 0
            if event == Engine.E_READABLE:
                eventmask = select.POLLIN
            elif event == Engine.E_WRITABLE:
                eventmask = select.POLLOUT
            self.polling.register(fd, eventmask)
        else:
            self.polling.unregister(fd)

    def set_reading(self, client):
        """
        Set client reading state.
        """
        # listen for readable events
        self.modify(client, Engine.E_READABLE, 0)

    def set_writing(self, client):
        """
        Set client writing state.
        """
        # listen for writable events
        self.modify(client, Engine.E_WRITABLE, 0)

    def runloop(self, timeout):
        """
        Pdsh engine run(): start clients and properly get replies
        """
        if timeout == 0:
            timeout = -1

        start_time = time.time()

        # run main event loop...
        while self.evlooprefcnt > 0:
            self._debug("LOOP evlooprefcnt=%d (reg_clifds=%s) (timers=%d)" % \
                    (self.evlooprefcnt, self.reg_clifds.keys(), len(self.timerq)))
            try:
                timeo = self.timerq.nextfire_delay()
                if timeout > 0 and timeo >= timeout:
                    # task timeout may invalidate clients timeout
                    self.timerq.clear()
                    timeo = timeout
                elif timeo == -1:
                    timeo = timeout

                self.reg_clifds_changed = False
                evlist = self.polling.poll(timeo * 1000.0 + 1.0)

            except select.error, (ex_errno, ex_strerror):
                # might get interrupted by a signal
                if ex_errno == errno.EINTR:
                    continue
                elif ex_errno == errno.EINVAL:
                    print >>sys.stderr, \
                            "EnginePoll: please increase RLIMIT_NOFILE"
                raise

            for fd, event in evlist:

                if event & select.POLLNVAL:
                    raise EngineException("Caught POLLNVAL on fd %d" % fd)

                if self.reg_clifds_changed:
                    self._debug("REG CLIENTS CHANGED - Aborting current evlist")
                    # Oops, reconsider evlist by calling poll() again.
                    break

                # get client instance
                if not self.reg_clifds.has_key(fd):
                    continue

                client = self.reg_clifds[fd]

                # process this client
                client._processing = True

                # check for poll error condition of some sort
                if event & select.POLLERR:
                    self._debug("POLLERR %s" % client)
                    self.unregister_writer(client)
                    client.file_writer.close()
                    client.file_writer = None
                    continue

                # check for data to read
                if event & select.POLLIN:
                    assert client._events & Engine.E_READABLE
                    self.modify(client, 0, Engine.E_READABLE)
                    try:
                        client._handle_read()
                    except EngineClientEOF, e:
                        self._debug("EngineClientEOF %s" % client)
                        self.remove(client)
                        continue

                # or check for end of stream (do not handle both at the same time
                # because handle_read() may perform a partial read)
                elif event & select.POLLHUP:
                    self._debug("POLLHUP fd=%d %s (r%s,w%s)" % (fd, client.__class__.__name__,
                        client.reader_fileno(), client.writer_fileno()))
                    self.remove(client)

                # check for writing
                if event & select.POLLOUT:
                    self._debug("POLLOUT fd=%d %s (r%s,w%s)" % (fd, client.__class__.__name__,
                        client.reader_fileno(), client.writer_fileno()))
                    assert client._events & Engine.E_WRITABLE
                    self.modify(client, 0, Engine.E_WRITABLE)
                    client._handle_write()

                # post processing
                client._processing = False

                # apply any changes occured during processing
                if client.registered:
                    self.set_events(client, client._new_events)

            # check for task runloop timeout
            if timeout > 0 and time.time() >= start_time + timeout:
                raise EngineTimeoutException()

            # process clients timeout
            self.fire_timers()

        self._debug("LOOP EXIT evlooprefcnt=%d (reg_clifds=%s) (timers=%d)" % \
                (self.evlooprefcnt, self.reg_clifds, len(self.timerq)))

    def exited(self):
        """
        Returns True if the engine has exited the runloop once.
        """
        return not self.running and self.exited

    def join(self):
        """
        Block calling thread until runloop has finished.
        """
        self.start_lock.acquire()
        self.start_lock.release()
        self.run_lock.acquire()
        self.run_lock.release()

