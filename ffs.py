#!/usr/bin/env python

import argparse, os, signal, socket, sys, tempfile
from gi.repository import GObject, Gtk, GLib, GUPnPIgd, Pango, Soup

class IPState:
    UNKNOWN = 0
    AVAILABLE = 1
    UNAVAILABLE = 2

class SharedFileState:
    BROKEN = 0
    PREPARING = 1
    READY = 2

class FancyFileServer (Gtk.Window):

    def __init__ (self, files, port, allow_uploads):
        Gtk.Window.__init__ (self, title = "Fancy File Server")
        
        self.config_port = port
        self.server_header = "fancy-file-server"
        self.have_7z = GLib.find_program_in_path ("7z")

        self.igd = None
        self.allow_upload = allow_uploads

        self.shared_file = None
        self.shared_content = None
        self.shared_file_is_temporary = False
        self.shared_file_state = SharedFileState.BROKEN

        self.request_count = 0
        self.request_finished_count = 0

        self.connect("delete_event", self.delete_event)

        self.set_default_size (400, 200)

        hbox = Gtk.HBox (spacing = 6)
        self.add (hbox)

        vbox = Gtk.VBox (spacing = 12)
        hbox.pack_start (vbox, True, False, 0)

        self.share_button = Gtk.Button ()
        self.share_button.connect ("clicked", self.on_button_clicked)
        vbox.pack_start(self.share_button, False, False, 0)

        self.sharing_label = Gtk.Label ("")
        self.sharing_label.set_ellipsize (Pango.EllipsizeMode.END)
        vbox.pack_start (self.sharing_label, False, False, 0)

        self.address_label = Gtk.Label ("")
        self.address_label.set_selectable (True)
        vbox.pack_start (self.address_label, False, False, 0)

        self.info_label = Gtk.Label ("")
        vbox.pack_start (self.info_label, False, False, 0)

        hbox = Gtk.HBox (spacing = 6)
        vbox.pack_start (hbox, False, False, 0)

        label = Gtk.Label ("Allow uploads:")
        hbox.pack_start (label, False, False, 0)

        self.upload_switch = Gtk.Switch ()
        self.upload_switch.set_active (self.allow_upload)
        hbox.pack_start (self.upload_switch, False, False, 0)
        self.upload_switch.connect ("notify::active", self.on_upload_switch_notify)

        self.start_server ()

        if (len (files) > 0):
            self.start_sharing (files)

        self.update_ui ()


    def on_upload_switch_notify (self, switch, spec):
        self.allow_upload = self.upload_switch.get_active()


    def delete_event(self, widget, event, data = None):
        self.stop_server ()
        return False


    def start_server (self):
        self.local_ip = None
        self.local_port = None
        self.local_ip_state = IPState.UNKNOWN

        self.upnp_ip = None
        self.upnp_port = None
        self.upnp_ip_state = IPState.UNKNOWN

        self.server = GObject.new (Soup.Server,
                                   port = self.config_port,
                                   server_header = self.server_header)
        if (self.server == None):
            # TODO: error?
            return

        self.local_ip = self.find_ip ()
        self.local_port = self.server.get_port ()
        self.server.add_handler (None, self.on_download_request, None)
        self.server.add_handler ("/u", self.on_upload_request, None)
        self.server.connect ("request-finished", self.on_soup_request_finished)
        print "Server starting, guessed uri http://%s:%d" % (self.local_ip, self.local_port)
        self.server.run_async ()

        # Is URI really available (at least from this machine)?
        self.confirm_uri (self.local_ip, self.local_port, False)

        try:
            self.igd = GUPnPIgd.SimpleIgd ()
            self.igd.connect ("mapped-external-port", self.on_igd_mapped_port)
            # Broken: python/GI can't cope with signals with GError
            # self.igd.connect ("error-mapping-port", self.on_igd_error)
            self.igd.add_port ("TCP",
                               self.local_port, # remote port really
                               self.local_ip, self.local_port,
                               0, "my-first-file-server")
        except:
            self.upnp_ip_state = IPState.UNAVAILABLE
            print "Failed to add UPnP port mapping"


    def stop_server (self):
        self.stop_sharing ()

        if (self.igd):
            self.igd.remove_port ("TCP", self.local_port)
            self.igd = None

        if (self.server):
            self.server.disconnect()
            self.server = None


    # Utility function to guess the IP (as a string) where the server can be
    # reached from the outside. Quite nasty problem actually.
    # Copied from http://www.home.unix-ag.org/simon/woof, GPL 2+
    def find_ip (self):
        # we get a UDP-socket for the TEST-networks reserved by IANA.
        # It is highly unlikely, that there is special routing used
        # for these networks, hence the socket later should give us
        # the ip address of the default route.
        # We're doing multiple tests, to guard against the computer being
        # part of a test installation.

        candidates = []
        for test_ip in ["192.0.2.0", "198.51.100.0", "203.0.113.0"]:
            s = socket.socket (socket.AF_INET, socket.SOCK_DGRAM)
            s.connect ((test_ip, 80))
            ip_addr = s.getsockname ()[0]
            s.close ()
            if ip_addr in candidates:
                return ip_addr
            candidates.append (ip_addr)

        return candidates[0]


    def update_ui (self):
        if (self.shared_file == None):
            self.share_button.set_label ("Share a file")
            self.sharing_label.set_text ("Currently not sharing anything.")
            self.address_label.set_text ("")
            self.info_label.set_text ("")
            return

        self.share_button.set_label ("Stop sharing")

        if (self.shared_file_state == SharedFileState.BROKEN):
            self.sharing_label.set_text ("Failed to share '%s', sorry." % GLib.path_get_basename (self.shared_file))
            self.address_label.set_text ("")
            self.info_label.set_text ("")
            return

        if (self.shared_file_state == SharedFileState.PREPARING):
            self.sharing_label.set_text ("Now preparing '%s' for sharing at" % GLib.path_get_basename (self.shared_file))
        else:
            self.sharing_label.set_text ("Now Sharing '%s' at" % GLib.path_get_basename (self.shared_file))
        self.address_label.set_text ("%s:%d" % (self.local_ip, self.server.get_port()))
        self.address_label.select_region (0, -1)
        if (self.request_count == 0):
            self.info_label.set_text ("It has not been downloaded yet.")
        elif (self.request_finished_count == 0):
            self.info_label.set_text ("It is being downloaded now.")
        elif (self.request_finished_count == self.request_count):
            if (self.request_finished_count == 1):
                self.info_label.set_text ("It has been downloaded once.")
            else:
                self.info_label.set_text ("It has been downloaded %d times." % self.request_finished_count)
        else:
            if (self.request_finished_count == 1):
                self.info_label.set_text ("It is being downloaded now and has been downloaded once already.")
            else:
                self.info_label.set_text ("It is being downloaded now and has been downloaded %d times already." % self.request_finished_count)

    def get_upload_form (self):
        form = """<!DOCTYPE html PUBLIC \"-//W3C//DTD XHTML 1.0 Strict//EN\" \"http://www.w3.org/TR/xhtml1/DTD/xhtml1-strict.dtd\">
<html>
<head><title>Friendly File Uploader</title><meta http-equiv=\"content-type\" content=\"text/html; charset=utf-8\" /></head>
<body><h1>Hello, please upload a file</h1>
<form action="/u" enctype="multipart/form-data" method="post">
<input type="file" name="file" size="40">
<input type="submit" value="Send">
</form>
</body>
</html>"""
        return form

    def header_print (self, name, value, data):
        print name, value

    def on_upload_request (self, server, message, path, query, client, wtf_is_this):
        if (message.method != "GET" and message.method != "HEAD" and message.method != "POST"):
            message.set_status (Soup.KnownStatusCode.METHOD_NOT_ALLOWED)
            return 

        if (path != "/u"):
            message.set_status (Soup.KnownStatusCode.NOT_FOUND)
            return

        if (message.method == "POST"):
            # upload coming through ...
            
            mp = Soup.Multipart.new_from_message (message.request_headers,
                                                  message.request_body)
            [has_part, header, body] = mp.get_part (0)
            if (not has_part):
                message.set_status (Soup.KnownStatusCode.BAD_REQUEST)
                # TODO: error message
                return

            [has_cd, cd, params] = header.get_content_disposition ()

            basename = params["filename"]
            path = GLib.get_user_special_dir (GLib.UserDirectory.DIRECTORY_DOWNLOAD)

            if (basename == None):
                filename = "%s/uploaded_file" % path
            else:
                filename = "%s/%s" % (path, basename)

            # TODO fix file name clash

            try:
                with open(filename, "w") as f:
                    f.write (body.get_data ())
                message.set_status (Soup.KnownStatusCode.OK)
            except:
                print "Attempted upload failed"
                message.set_status (Soup.KnownStatusCode.INTERNAL_SERVER_ERROR)
            return

        # method is GET or HEAD, return the upload form
        form = self.get_upload_form ()
        message.set_response ("text/html", Soup.MemoryUse.COPY, form)
        message.set_status (Soup.KnownStatusCode.OK)


    def on_download_request (self, server, message, path, query, client, wtf_is_this):
        if (message.method != "GET" and message.method != "HEAD"):
            message.set_status (Soup.KnownStatusCode.METHOD_NOT_ALLOWED)
            return 

        if (self.shared_file == None or path != "/"):
            message.set_status (Soup.KnownStatusCode.NOT_FOUND)
            return

        if (message.method == "HEAD"):
            # this is for confirm_uri() mostly
            message.set_status (Soup.KnownStatusCode.OK)
            return

        if (self.shared_content == None):
            try:
                self.shared_content = GLib.file_get_contents (self.shared_file)[1]
            except:
                print "Failed to get contents of '%s' while handling request." % self.shared_file
                message.set_status (Soup.KnownStatusCode.INTERNAL_SERVER_ERROR)
                self.shared_file_state = SharedFileState.BROKEN
                self.update_ui ()
                return

        message.set_status (Soup.KnownStatusCode.OK)

        attachment = {"filename": GLib.path_get_basename (self.shared_file)}
        message.response_headers.set_content_disposition ("attachment", attachment)
        message.response_body.append_buffer (Soup.Buffer.new (self.shared_content))

        self.request_count += 1
        self.update_ui ()


    def on_soup_request_finished (self, server, message, client):
        if (message.status_code == Soup.KnownStatusCode.OK and
            message.method == "GET"):
            self.request_finished_count += 1
            self.update_ui ()


    def on_test_response (self, session, message, is_upnp):
        state = IPState.UNAVAILABLE
        if (message.response_headers.get_one ("server") == self.server_header):
            state = IPState.AVAILABLE

        if (is_upnp):
            self.upnp_ip_state = state
        else:
            self.local_ip_state = state


    def confirm_uri (self, ip, port, is_upnp):
        uri = Soup.URI ()
        uri.set_scheme ("http")
        uri.set_host (ip)
        uri.set_path ("/")
        uri.set_port (port)

        msg = Soup.Message()
        msg.set_property ("uri", uri)
        msg.set_property ("method", "HEAD")

        session = Soup.SessionSync ()
        session.queue_message (msg, self.on_test_response, is_upnp)


    def on_igd_error (self, igd, err, proto, ep, lip, lp, msg):
        print "UPnP port forwarding failed"
        self.upnp_ip_state = IPState.UNAVAILABLE


    def on_igd_mapped_port (self, igd, proto,
                            ext_ip, old_ext_ip, ext_port,
                            local_ip, local_port,
                            desc):
        print "NAT punched at http://%s:%d" % (ext_ip, ext_port)
        self.upnp_ip = ext_ip
        self.upnp_port = ext_port
        self.upnp_ip_state = IPState.UNKNOWN
        self.confirm_uri (ext_ip, ext_port, True)


    def start_sharing (self, files):
        if (self.shared_file != None):
            self.stop_sharing ()

        if (len (files) > 1 or GLib.file_test (files[0], GLib.FileTest.IS_DIR)):
            self.shared_file_is_temporary = True
            self.shared_file_state = SharedFileState.PREPARING
            self.shared_file = self.create_temporary_archive (files)
        elif (len (files) == 1):
            self.shared_file_is_temporary = False
            self.shared_file_state = SharedFileState.READY
            self.shared_file = files[0]

        if (self.shared_file == None):
            self.shared_file_state = SharedFileState.BROKEN
            return

        self.shared_content = None
        self.request_count = 0
        self.request_finished_count = 0

        self.update_ui ()


    def stop_sharing (self):
        if (self.shared_file_is_temporary):
            try:
                os.remove (self.shared_file)
                os.rmdir (GLib.path_get_dirname (self.shared_file))
            except :
                print "Failed to remove temporary file"

        self.shared_file = None
        self.shared_content = None

        self.update_ui ()


    def on_child_process_exit (self, pid, status):
        GLib.spawn_close_pid (pid)
        wexitstatus = os.WEXITSTATUS (status)
        if (wexitstatus == 0):
            self.shared_file_state = SharedFileState.READY
        elif (wexitstatus == 1):
            self.shared_file_state = SharedFileState.READY
            print ("7z returned 1 (warning), but created the archive.")
        else:
            self.shared_file_state = SharedFileState.BROKEN
            print ("oops, 7z returned %s" % wexitstatus)

        self.update_ui ()


    def create_temporary_archive (self, files):
        temp_dir = tempfile.mkdtemp ("", "ffs-")
        if (len (files) == 1):
            archive_name = "%s/%s.zip" % (temp_dir, GLib.path_get_basename (files[0]))
        else:
            archive_name = "%s/archive.zip" % temp_dir

        cmd = ["7z",
               "-y", "-tzip", "-bd", "-mx=9",
               "a", archive_name,
               "--"]
        flags = GLib.SpawnFlags.SEARCH_PATH | GLib.SpawnFlags.DO_NOT_REAP_CHILD | GLib.SpawnFlags.STDOUT_TO_DEV_NULL
        try:
            [pid, i, o, e] = GLib.spawn_async (cmd + files, [],
                                               GLib.get_current_dir (), flags)
            GLib.child_watch_add (pid, self.on_child_process_exit)
            return archive_name
        except GLib.Error as e:
            print "Failed to spawn 7z: %s" % e.message
            return None

    def on_button_clicked (self, widget):
        if (self.shared_file != None):
            self.stop_sharing ()
        else:
            dialog = Gtk.FileChooserDialog ("Select files or folders to share", self,
                                            Gtk.FileChooserAction.OPEN,
                                            (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                                             "Share", Gtk.ResponseType.OK))
            dialog.set_select_multiple (self.have_7z)
            if (dialog.run () == Gtk.ResponseType.OK):
                files = dialog.get_filenames ()
                self.start_sharing (files)

            dialog.destroy()


def ensure_positive (value):
    try:
        v = int (value)
    except Exception:
        raise argparse.ArgumentTypeError("Port must be a positive integer")
    if (v < 0):
        raise argparse.ArgumentTypeError("Port must be a positive integer")
    return v


# https://bugzilla.gnome.org/show_bug.cgi?id=622084
signal.signal(signal.SIGINT, signal.SIG_DFL)

parser = argparse.ArgumentParser(description="Share files on the internet.")
parser.add_argument ("file", nargs = "*", help="file that should be shared")
parser.add_argument ("-p", "--port", type = ensure_positive, default = 0)
parser.add_argument ("-u", "--allow-uploads", action = "store_true")
args = parser.parse_args ()

win = FancyFileServer (args.file, args.port, args.allow_uploads)
win.connect ("delete-event", Gtk.main_quit)
win.show_all ()
Gtk.main ()
