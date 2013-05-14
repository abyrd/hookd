#!/usr/bin/python

import time, json, sys, cgi, traceback
from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
import Queue, threading, syslog, subprocess, os

PORT = 8888
BASE_DIR = '/home/abyrd/'
DAEMON = False
N_WORKER_THREADS = 2
WORK_SCRIPT = ''
LOG_IDENT = 'otphook'

# global state used by all threads
# http://docs.python.org/2/library/threading.html#module-threading (Condition Objects)
shutdown = threading.Event()
q = Queue.Queue(100)
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
    def call (self, cmd) :
        info ("calling '%s'" % cmd)
        result = subprocess.call( cmd.split(), stdout=self.log, stderr=self.log )
        info ("result of '%s' was %s" % (cmd, 'OK' if result == 0 else 'FAIL'))       
    def do_work (self, work_unit) :
        repo_name, repo_url, head_commit = work_unit
        dir = BASE_DIR + '/thread_%d_workspace' % self.threadID
        try :
            os.chdir (dir)
        except :
            info ('thread workspace directory does not exist, creating')
            os.mkdir (dir)
            os.chdir (dir)
        try :
            os.chdir (repo_name)
        except :
            info ('repository does not exist, cloning')
            result = subprocess.call( ['git', 'clone', repo_url, repo_name], stdout=sys.stdout )
            info ('result of clone operation was %d' % result)
            os.chdir (repo_name)
        self.log = open ('%s/build_log_%s' % (dir, head_commit), 'w')
        self.call ('git fetch')
        self.call ('git clean -f')
        self.call ('git checkout ' + head_commit)
        self.call( 'mvn clean package' )
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
        
        
def main():  
    global q, qlock, shutdown
    log_options = (0 if DAEMON else syslog.LOG_PERROR) | syslog.LOG_PID 
    syslog.openlog(LOG_IDENT, logoption=log_options, facility=syslog.LOG_DAEMON)
    workerThreads = []
    for thread_id in range(N_WORKER_THREADS) :
        thread = WorkerThread(thread_id)
        workerThreads.append(thread)
        thread.start()
    info ('Spawned %d worker threads.' % N_WORKER_THREADS)
    info ('Starting HTTP Github commit hook handler.')
    server = HTTPServer(('', PORT), HookHandler)
    try:
        server.serve_forever()
        #double-fork?
    except KeyboardInterrupt:
        info ('SIGINT received, github hook handler shutting down.')
        server.socket.close()
        # notify worker threads that we are shutting down
        shutdown.set()
        qlock.acquire()
        qlock.notifyAll() # wake up all threads waiting on the queue
        qlock.release()
        for thread in workerThreads:
            thread.join(1)
        info ("All worker threads exited, now exiting main thread.")
        syslog.closelog()
        
if __name__ == '__main__':
    main()


