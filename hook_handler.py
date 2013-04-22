#!/usr/bin/python


import time, json, sys, cgi
from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer

PORT = 8888
REPO = 'openplans/OpenTripPlanner'
DAEMON = False

# check slowness with curl -v

# github sends urlencoded form in the body
# with payload={json}
# i.e. content-type is application/x-www-form-urlencoded

class HookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            # time.sleep(2) # checked, requests are queued and handled sequentially
            length = int(self.headers.getheader('content-length'))
            print 'received message header indicating message of length', length
            # proper clients (like curl) wait for 100 to send POST content to ensure that request is accepted
            self.send_response(100) 
            self.end_headers()
            # client has now received 100, will send body
            body = self.rfile.read(length)
            postvars = cgi.parse_qs(body)
            j = json.loads(postvars['payload'])
            commit = j['after']
            print 'received notification from Github of commit', commit
            self.wfile.write('thank you for your patronage.\n')
        except Exception as e:
            self.wfile.write('i failed to understand your message.\n')
            print sys.exc_info()
    
def main():
    try:
        server = HTTPServer(('', PORT), HookHandler)
        print 'Started HTTP Github commit hook handler'
        server.serve_forever()
        #double-fork?
    except KeyboardInterrupt:
        print 'SIGINT received, shutting down'
        server.socket.close()

if __name__ == '__main__':
    main()


