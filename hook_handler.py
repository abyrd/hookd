#!/usr/bin/python

import time, json, sys, cgi, traceback
from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
import Queue, threading, syslog, subprocess, os

# debian package python-daemon
import daemon, daemon.pidlockfile, signal

# NOTE github has a "test hook" button, see https://github.com/abyrd/hooktest/settings/hooks

PORT = 8888
BASE_DIR = '/var/opt/githook/'
WORK_DIR = BASE_DIR
LOG_DIR = BASE_DIR
N_WORKER_THREADS = 2
WORK_SCRIPT = ''
LOG_IDENT = 'githook'

# global state used by all threads
# http://docs.python.org/2/library/threading.html#module-threading (Condition/Event Objects)
shutdown = threading.Event()
q = Queue.Queue() # unbounded queue
qlock = threading.Condition()
log_lock = threading.Lock()

def info (msg) :
    log_lock.acquire()
    syslog.syslog(syslog.LOG_INFO, msg)
    log_lock.release()

def debug (msg) :
    log_lock.acquire()
    syslog.syslog(syslog.LOG_DEBUG, msg)
    log_lock.release()

class WorkerThread (threading.Thread) :
    def __init__ (self, thread_id) :
        threading.Thread.__init__(self)
        self.threadID = thread_id
        self.name = 'thread %d' % thread_id
        self.dir = os.path.join(BASE_DIR, 'thread_%d_workspace' % self.threadID)
        # note: cannot change current directory per thread. multiprocessing?
        try :
            os.chdir (self.dir)
        except :
            info ('thread %s workspace directory does not exist, creating %s' % (self.threadID, self.dir))
            os.mkdir (self.dir)
            os.chdir (self.dir)
    def run (self) :
        global q, qlock, shutdown
        while True :
            qlock.acquire()
            if q.empty():
                debug ('%s going to sleep' % self.name)
                qlock.wait()
            info ('%s awake' % self.name)
            if shutdown.is_set() :
                debug ('%s exiting' % self.name)
                qlock.release()
                break
            else :
                work_unit = q.get()
                info ('thread %d got %s' % (self.threadID, work_unit))
                qlock.release()
                self.do_work (work_unit)
                # print self.name, 'got', commit_sha1
    def call (self, cmd, cwd) :
        info ("calling '%s'" % cmd)
        result = subprocess.call( cmd.split(), stdout=self.log, stderr=self.log, cwd=cwd )
        info ("result of '%s' was %s" % (cmd, 'OK' if result == 0 else 'FAIL'))
    def do_work (self, work_unit) :
        repo_name, repo_url, head_commit = work_unit
        repo_dir = os.path.join(self.dir, repo_name)
        try :
            os.chdir (repo_dir)
        except :
            info ('repository does not exist, cloning')
            result = subprocess.call( ['git', 'clone', repo_url, repo_name], stdout=sys.stdout, cwd=self.dir)
            info ('result of clone operation was %d' % result)
        self.log = open (os.join(LOG_DIR, 'build_log_' + head_commit), 'w')
        self.call ('git fetch', repo_dir)
        self.call ('git clean -f', repo_dir)
        self.call ('git checkout ' + head_commit, repo_dir)
        self.call( 'mvn clean package', repo_dir)
        self.log.close()
        
# from https://github.com/abyrd/hooktest/settings/hooks
# ALLOW_IPS = [207.97.227.253/32, 50.57.128.197/32, 108.171.174.178/32, 50.57.231.61/32, 204.232.175.64/27, 192.30.252.0/22]

# check slowness with curl -v

# github sends urlencoded form in the body
# with payload={json}
# i.e. content-type is application/x-www-form-urlencoded

class HookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        global q, qlock
        try:
            # time.sleep(2) # checked, requests are queued and handled sequentially
            length = int(self.headers.getheader('content-length'))
            ctype = self.headers.getheader('content-type')
            # print ctype
            info ('received message header indicating message of length %d' % length)
            # proper clients (like curl) wait for 100 to send POST content to ensure that request is accepted
            self.send_response(100) 
            self.end_headers()
            # client has now received 100, will send body
            body = self.rfile.read(length)
            # print body
            postvars = cgi.parse_qs(body)
            # print postvars
            j = json.loads(postvars['payload'][0])
            head_commit = j['head_commit']['id']
            repo_name = j['repository']['name']
            repo_url = j['repository']['url']
            info ('notification: commit to repository %s at %s' % (repo_name, repo_url))
            info ('notification: head commit sha1 is %s' % head_commit)
            self.wfile.write('thank you for your patronage.\n')
            qlock.acquire()
            q.put( (repo_name, repo_url, head_commit) )
            qlock.notify(1) # wake up one thread
            qlock.release() # release the lock so the thread can grab it
        except Exception as e:
            self.wfile.write('i failed to understand your message.\n')
            traceback.print_exc()
    def log_message(self, fmt, *args) :
        log_lock.acquire()
        syslog.syslog(syslog.LOG_ERR, fmt % args)
        log_lock.release()

daemonize = False
server=None
workerThreads = []
def terminate() :
    info ('terminate callback')
    if server is not None :
        server.socket.close()
    global q, qlock, shutdown
    shutdown.set() # notify worker threads that we are shutting down
    qlock.acquire()
    qlock.notifyAll() # wake up all threads waiting on the queue
    qlock.release()
    for thread in workerThreads:
        thread.join(1)
    info ("All worker threads exited, now exiting main thread.")
    log_lock.acquire()
    syslog.closelog()
    sys.exit(0)

def main() :  
    log_options = (0 if daemonize else syslog.LOG_PERROR) | syslog.LOG_PID 
    log_facility = syslog.LOG_DAEMON if daemonize else syslog.LOG_USER
    syslog.openlog(LOG_IDENT, logoption=log_options, facility=log_facility)
    for thread_id in range(N_WORKER_THREADS) :
        thread = WorkerThread(thread_id)
        workerThreads.append(thread)
        thread.start()
    info ('Spawned %d worker threads.' % N_WORKER_THREADS)
    info ('Starting HTTP Github commit hook handler.')
    try :
        server = HTTPServer(('', PORT), HookHandler)
        server.serve_forever()
    except KeyboardInterrupt :
        # SIGINT perhaps via ^C
        info ('SIGINT received, github hook handler shutting down.')
    except SystemExit : 
        # SystemExit is raised by daemon library on receiving SIGTERM
        info ('SIGTERM received, github hook handler shutting down.')
    except Exception as e:
        info ('Server failed: %s' % e)
    terminate()

def check_dirs () :
    for directory in [BASE_DIR, WORK_DIR, LOG_DIR] :
        if not os.path.isdir(directory) :
            print "directory '%s' does not exist" % directory
            exit(1)
        if not os.path.isdir(directory) :
            print "file '%s' is not a directory" % directory
            exit(1)
        if not os.access(directory, os.R_OK) :
            print "cannot read directory %'s'" % directory
            exit(1)
        if not os.access(directory, os.W_OK) :
            print "cannot write directory '%s'" % directory
            exit(1)
            
if __name__ == '__main__' :
    if len(sys.argv) > 1 and sys.argv[1] == '-d' :
        daemonize = True
    check_dirs ()
    if daemonize :
        # lockfile support seems to be screwed up
        # does not prevent multiple copies running
        # pidfile=daemon.pidlockfile.PIDLockFile('/var/run/githook.pid'),
        context = daemon.DaemonContext(working_directory = WORK_DIR)
        with context :
            main()
    else :
        main()

