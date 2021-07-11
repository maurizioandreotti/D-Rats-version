#!/usr/bin/python
'''Session Manager'''
#
# Copyright 2008 Dan Smith <dsmith@danplanet.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import absolute_import
from __future__ import print_function

import time
import threading
# import os
# import struct
# import socket
from six.moves import range

from . import transport
# from .ddt2 import DDT2EncodedFrame

from .sessions import base, control, stateful, stateless
# from .sessions import file, form, sock, sniff
# importing printlog() wrapper
from .debug import printlog


# pylint: disable=too-many-instance-attributes
class SessionManager():
    '''
    Session Manager.

    :param pipe:
    '''

    def set_comm(self, pipe, **kwargs):
        '''
        Set Comm

        :param pipe: pipe for communication
        :param station: Station for connection
        :param kwargs: Key word arguments
        '''
        self.pipe = pipe
        if self.tport:
            self.tport.disable()

        self.tport = transport.Transporter(self.pipe,
                                           inhandler=self.incoming,
                                           **kwargs)

    def set_call(self, callsign):
        '''
        Set callsign

        :param callsign: Set the callsign
        '''
        self.station = callsign

    def __init__(self, pipe, station, **kwargs):
        self.pipe = self.tport = None
        self.station = station

        self.sniff_session = None

        self.last_frame = 0
        self.sessions = {}
        self.session_cb = {}

        self.set_comm(pipe, **kwargs)

        self._sid_counter = 0
        self._sid_lock = threading.Lock()

        self.control = control.ControlSession()
        self._register_session(self.control, "CQCQCQ", "new,out")

        self._stations_heard = {}

    def get_heard_stations(self):
        '''
        Get Heard Stations

        :returns: Dict of stations heard
        '''
        return dict(self._stations_heard)

    def manual_heard_station(self, station):
        '''
        Manual Heard Station

        :param station: Station to update
        '''
        self._stations_heard[station] = time.time()

    def fire_session_cb(self, session, reason):
        '''
        Fire Session call back.

        :param session: Session for call back
        :param reason: Reason for callback
        '''
        for function, data in self.session_cb.items():
            try:
                function(data, reason, session)
            # pylint: disable=broad-except
            except Exception as err:
                printlog("Sessionmgr",
                         ": Exception in session CB: %s -%s-" %
                         (type(err), err))

    def register_session_cb(self, function, data):
        '''
        Register Session Call Back.

        :param function: Function to call back
        :param data: Data for call back function
        '''
        self.session_cb[function] = data

        for _item, session in self.sessions.items():
            self.fire_session_cb(session, "new,existing")

    def shutdown(self, force=False):
        '''
        Shutdown Session

        :param force: Optional True to force a shutdown
        '''
        if force:
            self.tport.disable()

        # pylint: disable=protected-access
        if self.control._id in self.sessions:
            # pylint: disable=protected-access
            del self.sessions[self.control._id]

        for session in self.sessions.values():
            printlog("Sessionmgr",
                     ": Stopping session `%s'" % session.name)
            session.close(force)

        if not force:
            self.tport.disable()

    def incoming(self, frame):
        '''
        Incoming Session Frame

        :param frame: Received frame
        '''
        # manage incoming sessions
        # record time for marking sessions
        self.last_frame = time.time()

        if frame.s_station not in ["!"]:
            # if new station let's assign the time to its name
            self._stations_heard[frame.s_station] = time.time()

        if self.sniff_session is not None: #sniff if asked to
            self.sessions[self.sniff_session].handler(frame)

        if frame.d_station != "CQCQCQ" and \
                frame.d_station != self.station and \
                frame.session != 1:
            # Not CQ, not us, and not chat
            printlog("Sessionmgr",
                     ": Received frame for station `%s'" % frame.d_station)
            return
        if frame.s_station == self.station:
            # Either there is another station using our callsign, or
            # this packet arrived back at us due to a loop
            printlog("Sessionmgr", ": Received looped frame")
            return
        #
        #mmmmmm
        # NOTE it could be here we have to add the rs-ms1a packets decoding ??
        #      but seems quite late because all portions of the packet seems
        #      already split
        # sample rs-ms1a string CQCQCQ: $$Msg,IZ2LXI,,0011DCtest transmission
        # self.emit("user-send-chat", "CQCQCQ", port,
        #           "$$Msg,IZ000,,0011D,%s" % d, True)

        if not frame.session in self.sessions:
            printlog("Sessionmgr",
                     ": Incoming frame for unknown session `%i'" %
                     frame.session)
            return

        session = self.sessions[frame.session]

        # pylint: disable=protected-access
        if not session.stateless and session._st != frame.s_station:
            printlog("Sessionmgr",
                     ": Received frame from invalid station `%s' "
                     "(expecting `%s'" % (frame.s_station, session._st))
            return

        if session.handler:
            session.handler(frame)
        else:
            session.inq.enqueue(frame)
            session.notify()

        printlog("Sessionmgr",
                 ": Received block %i:%i for session `%s'" %
                 (frame.seq, frame.type, session.name))

    def outgoing(self, session, block):
        '''
        Outgoing

        :param session: Session to use
        :param block: Block for sending
        '''
        self.last_frame = time.time()

        if not block.d_station:
            # pylint: disable=protected-access
            block.d_station = session._st

        block.s_station = self.station

        # pylint: disable=protected-access
        if session._rs:
            # pylint: disable=protected-access
            block.session = session._rs
        else:
            # pylint: disable=protected-access
            block.session = session._id

        self.tport.send_frame(block)

    def _get_new_session_id(self):
        self._sid_lock.acquire()
        if self._sid_counter >= 255:
            for ident in range(0, 255):
                if ident not in list(self.sessions.keys()):
                    self._sid_counter = ident
        else:
            ident = self._sid_counter
            self._sid_counter += 1

        self._sid_lock.release()

        return ident

    def _register_session(self, session, dest, reason):
        ident = self._get_new_session_id()
        if ident is None:
            # pylint: disable=fixme
            # FIXME
            printlog("Sessionmgr", ": No free slots?  I can't believe it!")

        # pylint: disable=protected-access
        session._sm = self
        # pylint: disable=protected-access
        session._id = ident
        # pylint: disable=protected-access
        session._st = dest
        self.sessions[ident] = session

        self.fire_session_cb(session, reason)

        return ident

    def _deregister_session(self, ident):
        '''
        Deregister Session

        :param ident: Identification of session to deregister
        '''
        if ident in self.sessions:
            self.fire_session_cb(self.sessions[ident], "end")

        try:
            del self.sessions[ident]
        # pylint: disable=broad-except
        except Exception as err:
            printlog("Sessionmgr:",
                     "No session %s to deregister (%s)" % (ident, err))

    def start_session(self, name, dest=None, cls=None, **kwargs):
        '''
        Start Session

        :param name: Name of session
        :param dest: Optional Destination of session
        :param cls: Optional Session class
        :param kwargs: Optional Key word arguments
        :returns: session object
        '''
        if not cls:
            if dest:
                session = stateful.StatefulSession(name)
            else:
                session = stateless.StatelessSession(name)
                dest = "CQCQCQ"
        else:
            session = cls(name, **kwargs)

        session.set_state(base.ST_SYNC)
        ident = self._register_session(session, dest, "new,out")

        if dest != "CQCQCQ":
            if not self.control.new_session(session):
                self._deregister_session(ident)

        return session

    def set_sniffer_session(self, ident):
        '''
        Set identity of sniffer session.

        :param ident: Identity to set
        '''
        self.sniff_session = ident

    def stop_session(self, session):
        '''
        Stop Session

        :param session: Session to stop
        :returns: True if session is found and stopped
        '''
        for ident, s_item in self.sessions.items():
            if s_item.name == s_item.name:
                self.tport.flush_blocks(ident)
                if session.get_state() != base.ST_CLSD:
                    self.control.end_session(session)
                self._deregister_session(ident)
                session.close()
                return True

        return False

    def end_session(self, ident):
        '''
        End Session.

        :param ident:  Session to end
        '''
        try:
            del self.sessions[ident]
        # pylint: disable=broad-except
        except Exception as err:
            printlog("Sessionmgr",
                     ": Unable to deregister session (%s -%s-)",
                     (type(err), err))

    def get_session(self, rid=None, rst=None, lid=None):
        '''
        Get Session

        :param rid: Optional receive ID
        :param rst: Optional rst value
        :param lid: Optional lid values
        :returns: Session object? or None
        '''
        if not (rid or rst or lid):
            printlog("Sessionmgr", ": get_station() with no selectors!")
            return None

        for session in self.sessions.values():
            # pylint: disable=protected-access
            if rid and session._rs != rid:
                continue

            # pylint: disable=protected-access
            if rst and session._st != rst:
                continue

            # pylint: disable=protected-access
            if lid and session._id != lid:
                continue

            return session

        return None

def main():
    '''Self test module'''
    # p = transport.TestPipe(dst="KI4IFW")

    from . import comm
    import sys
    from . import sessions

    # if sys.argv[1] == "KI4IFW":
    #     p = comm.SerialDataPath(("/dev/ttyUSB0", 9600))
    # else:
    #     p = comm.SerialDataPath(("/dev/ttyUSB0", 38400))

    p_test = comm.SocketDataPath(("localhost", 9000))
    #p.make_fake_data("SOMEONE", "CQCQCQ")
    p_test.connect()
    session_mgr = SessionManager(p_test, sys.argv[1])
    session = session_mgr.start_session("chat", dest="CQCQCQ",
                                        cls=sessions.chat.ChatSession)

    def call_back(_data, _args):
        printlog("Sessionmgr", ": ---------[ CHAT DATA ]------------")

    # pylint: disable=no-member
    session.register_cb(call_back)

    session.write("This is %s online" % sys.argv[1])

    if sys.argv[1] == "KI4IFW":
        file_class = sessions.file.FileTransferSession
        session2 = session_mgr.start_session("xfer", "KI4IFW",
                                             cls=file_class)
        session2.send_file("inputdialog.py")
    else:
        def h_call_back(_data, reason, session):
            printlog(("Sessionmgr: Session CB: %s" % reason))
            if reason == "new,in":
                printlog("Sessionmgr", ": Receiving file")
                thread = threading.Thread(target=session.recv_file,
                                          args=("/tmp",))
                thread.setDaemon(True)
                thread.start()
                printlog("Sessionmgr", ": Done")

        session_mgr.register_session_cb(h_call_back, None)

    try:
        while True:
            time.sleep(30)
    # pylint: disable=broad-except
    except Exception as err:
        printlog("Sessionmgr",
                 ": ------- Closing (%s -%s-)" % (type(err), err))

    session_mgr.shutdown()

#    blocks = s.recv_blocks()
#    for b in blocks:
#        printlog("Sessionmgr",
#                 ": Chat message: %s: %s" % (b.get_info()[2], b.get_data()))

if __name__ == "__main__":
    main()
