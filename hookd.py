#!/usr/bin/python

import time, json, sys, cgi, traceback
from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
import Queue, threading, syslog, subprocess, os

# debian package python-daemon
import daemon, daemon.pidlockfile, signal

# NOTE github has a "test hook" button, see https://github.com/abyrd/hooktest/settings/hooks

PORT = 8888
BASE_DIR = '/var/opt/hookd/'
WORK_DIR = BASE_DIR
LOG_DIR = BASE_DIR
N_WORKER_THREADS = 2
WORK_SCRIPT = ''
LOG_IDENT = 'hookd'
ACCEPT_REPOS = ['hookd']
ACCEPT_ACCOUNTS = ['abyrd', 'openplans']

# global state used by all threads
# http://docs.python.org/2/library/threading.html#module-threading (Condition/Event Objects)
shutdown = threading.Event()
q = Queue.Queue() # unbounded queue
qlock = threading.Condition()
log_lock = threading.Lock()

def error (msg) :
    log_lock.acquire()
    syslog.syslog(syslog.LOG_ERR, msg)
    log_lock.release()
    terminate(1)

def info (msg) :
    log_lock.acquire()
    syslog.syslog(syslog.LOG_INFO, msg)
    log_lock.release()

def debug (msg) :
    log_lock.acquire()
    syslog.syslog(syslog.LOG_DEBUG, msg)
    log_lock.release()

def check_url (url) :
    parts = url.split('/')
    if len(parts) != 5 :
        info ('received malformed repository URL: %s' % url)
        return False
    repo = parts[-1]
    acct = parts[-2]
    if not repo in ACCEPT_REPOS :
        info ('rejected repository name %s from URL %s' % (repo, url))
        return False
    if not acct in ACCEPT_ACCOUNTS :
        info ('rejected repository name %s from URL %s' % (acct, url))
        return False
    return True

class FailedCall (Exception) :
    def __init__ (self, code) :
        self.code = code
        self.message = code

class WorkerThread (threading.Thread) :
    def __init__ (self, thread_id) :
        threading.Thread.__init__(self)
        self.threadID = thread_id
        self.name = 'worker%d' % thread_id
        self.dir = os.path.join(BASE_DIR, '%s_workspace' % self.name)
        # note: cannot change current directory per thread. multiprocessing?
        try :
            os.chdir (self.dir)
        except :
            info ('workspace directory does not exist, creating %s' % self.dir)
            os.mkdir (self.dir)
            os.chdir (self.dir)
    def run (self) :
        global q, qlock, shutdown
        while True :
            qlock.acquire()
            if q.empty():
                # debug ('%s going to sleep' % self.name)
                qlock.wait()
            # info ('%s awake' % self.name)
            if shutdown.is_set() :
                debug ('%s exiting' % self.name)
                qlock.release()
                break
            else :
                work_unit = q.get()
                info ('%s: got work unit %s' % (self.name, work_unit))
                qlock.release()
                self.do_work (work_unit)
    def call (self, cmd, cwd=None) :
        if cwd is None :
            cwd = self.repo_dir
        if not isinstance(cmd, list) :
            cmd = cmd.split()
        info ("%s: calling '%s'" % (self.name, cmd))
        result = subprocess.call( cmd, stdout=self.log, stderr=self.log, cwd=cwd )
        info ("%s: result of '%s' was %s" % (self.name, cmd, 'OK' if result == 0 else 'FAIL'))
        if result != 0 :
            raise FailedCall(result)
    def do_work (self, work_unit) :
        repo_name, repo_url, head_commit = work_unit
        self.repo_dir = os.path.join(self.dir, repo_name)
        self.log = open (os.path.join(LOG_DIR, '%s_build_%s_%s.log' % (self.name, repo_name, head_commit)), 'w')
        try :
            try :
                os.chdir (self.repo_dir)
                info ('%s: repository directory %s already exists' % (self.name, self.repo_dir))
            except :
                self.call (['git', 'clone', repo_url, repo_name], cwd=self.dir)
            self.call ('git fetch')
            self.call ('git clean -f')
            self.call ('git checkout ' + head_commit)
            self.call( 'mvn clean package')
        except FailedCall :
            info ('%s: a build step failed, aborting.' % self.name)
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
            if check_url(repo_url) :
                qlock.acquire()
                q.put( (repo_name, repo_url, head_commit) )
                qlock.notify(1) # wake up one thread
                qlock.release() # release the lock so the thread can grab it
            else :
                self.wfile.write('why u send me junk?\n')
        except Exception as e:
            self.wfile.write('i failed to understand your message.\n')
            traceback.print_exc()
    def log_message(self, fmt, *args) :
        log_lock.acquire()
        syslog.syslog(syslog.LOG_ERR, fmt % args)
        log_lock.release()

server=None
workerThreads = []
def terminate(status=0) :
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
    sys.exit(status)

daemonize = False
def main() :  
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
            error ("directory '%s' does not exist" % directory)
        if not os.path.isdir(directory) :
            error ("file '%s' is not a directory" % directory)
        if not os.access(directory, os.R_OK) :
            error ("cannot read directory '%s'" % directory)
        if not os.access(directory, os.W_OK) :
            error ("cannot write directory '%s'" % directory)
            
if __name__ == '__main__' :
    if len(sys.argv) > 1 and sys.argv[1] == '-d' :
        daemonize = True
    log_options = (0 if daemonize else syslog.LOG_PERROR) | syslog.LOG_PID 
    log_facility = syslog.LOG_DAEMON if daemonize else syslog.LOG_USER
    syslog.openlog(LOG_IDENT, logoption=log_options, facility=log_facility)
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

