#!/usr/bin/env python

import os, signal, socket, sys, tempfile
from gi.repository import GObject, Gtk, GLib, GUPnPIgd, Pango, Soup


class FancyFileServer (Gtk.Window):
    def __init__ (self, filename):
        Gtk.Window.__init__ (self, title="Fancy File Server")

        self.port = 55555;

        self.server_header = "fancy-file-server"

        self.have_7z = GLib.find_program_in_path ("7z")

        self.set_default_size (400, 200)

        hbox = Gtk.HBox (spacing=6)
        self.add (hbox)

        vbox = Gtk.VBox (spacing=12)
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

        self.shared_file = None
        self.update_ui()

        if (filename != None):
            self.start_sharing (filename)


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
        else:
            self.share_button.set_label ("Stop sharing")
            self.sharing_label.set_text ("Now Sharing '{}' at".format (GLib.path_get_basename (self.shared_file)))
            self.address_label.set_text ("{}:{}".format (self.local_ip, self.server.get_port()))
            self.address_label.select_region (0, -1)
            if (self.request_count == 0):
                self.info_label.set_text ("It has not been downloaded yet.")
            elif (self.request_finished_count == 0):
                self.info_label.set_text ("It is being downloaded now.")
            elif (self.request_finished_count == self.request_count):
                if (self.request_finished_count == 1):
                    self.info_label.set_text ("It has been downloaded once.")
                else:
                    self.info_label.set_text ("It has been downloaded {} times.".format (self.request_finished_count))
            else:
                if (self.request_finished_count == 1):
                    self.info_label.set_text ("It is being downloaded now and has been downloaded once already.")
                else:
                    self.info_label.set_text ("It is being downloaded now and has been downloaded {} times already.".format (self.request_finished_count))


    def on_soup_request (self, server, message, path, query, client, wtf_is_this):
        if (message.method != "GET"):
            if (message.method == "HEAD"):
                message.set_status (Soup.KnownStatusCode.OK)
            else:
                message.set_status (Soup.KnownStatusCode.METHOD_NOT_ALLOWED)
            return 

        if (self.shared_content == None):
            try:
                self.shared_content = GLib.file_get_contents (self.shared_file)[1]
            except:
                message.set_status (Soup.KnownStatusCode.INTERNAL_SERVER_ERROR)
                print "Internal error: failed to get contents of '{}'".format (self.shared_file)
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
            print "request: finished"


    def on_test_response (self, session, message, data):
        if (message.response_headers.get_one ("server") == self.server_header):
            print "Uri seems to be available"


    def confirm_uri (self, ip, port):
        uri = Soup.URI ()
        uri.set_scheme ("http")
        uri.set_host (ip)
        uri.set_path ("/")
        uri.set_port (port)

        msg = Soup.Message()
        msg.set_property ("uri", uri)
        msg.set_property ("method", "HEAD")

        session = Soup.SessionSync ()
        print "Testing URI {} ...".format (uri.to_string(False))
        session.queue_message (msg, self.on_test_response, None)


    def on_igd_mapped_port (self, igd, proto,
                            ext_ip, old_ext_ip,
                            ext_port, local_ip,
                            desc, data):
        print "NAT punched at http://{}:{}".format (ext_ip, ext_port)
        self.confirm_uri (ext_ip, ext_port)

    def start_sharing (self, filename, is_temporary):
        self.shared_file = filename
        self.shared_content = None
        self.shared_file_is_temporary = is_temporary

        self.local_ip = self.find_ip ()
        self.request_count = 0
        self.request_finished_count = 0

        self.server = GObject.new (Soup.Server,
                                   port = self.port,
                                   server_header = self.server_header)
        if (self.server == None):
            print "Failed to start server"
            return
        self.server.add_handler (None, self.on_soup_request, None)
        self.server.connect ("request-finished", self.on_soup_request_finished)
        print "Server starting, guessed uri http://{}:{}".format(self.local_ip, self.server.get_port ())
        self.server.run_async ()


        # Make sure the URI is really available (at least from this
        # machine).
        self.confirm_uri (self.local_ip, self.server.get_port ())

        self.update_ui()

        self.igd = GUPnPIgd.SimpleIgd ()
        self.igd.connect ("mapped-external-port", self.on_igd_mapped_port)
        self.igd.add_port ("TCP",
                           self.server.get_port (), # remote port
                           self.local_ip,
                           self.server.get_port (),
                           0,
                           "my-first-file-server");


    def stop_sharing (self):
        if (self.shared_file_is_temporary):
            os.remove (self.shared_file);
            os.rmdir (GLib.path_get_dirname (self.shared_file))

        self.shared_file = None
        self.shared_content = None

        self.igd.remove_port ("TCP", self.server.get_port ())
        self.igd = None

        self.server.disconnect()
        self.server = None

        self.update_ui()


    def create_temporary_archive (self, files):
        try:
            temp_dir = tempfile.mkdtemp ("", "ffs-")
            if (len (files) == 1):
                archive_name = "{}/{}.zip".format (temp_dir, GLib.path_get_basename (files[0]))
            else:
                archive_name = "{}/archive.zip".format (temp_dir)

            cmd = ["7z", 
                   "-y", "-tzip", "-bd", "-mx=9", 
                   "a", archive_name,
                   "--"]
            GLib.spawn_async (cmd + files, [], temp_dir, GLib.SpawnFlags.SEARCH_PATH);
            return archive_name
        except GLib.Error:
            print "Failed to create a temporary archive"
            return None

    def on_button_clicked (self, widget):
        if (self.shared_file != None):
            self.stop_sharing ()
        else:
            dialog = Gtk.FileChooserDialog ("Select files or folders to share", self,
                                            Gtk.FileChooserAction.OPEN,
                                            (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                                             "Share", Gtk.ResponseType.OK))
            dialog.set_select_multiple (self.have_7z);
            if (dialog.run () == Gtk.ResponseType.OK):
                files = dialog.get_filenames ()
                if (len (files) > 1 or GLib.file_test (files[0], GLib.FileTest.IS_DIR)):
                    filename = self.create_temporary_archive (files)
                    self.start_sharing (filename, True)
                else:
                    self.start_sharing (files[0], False)

            dialog.destroy()


signal.signal(signal.SIGINT, signal.SIG_DFL)

filename = None
if (len (sys.argv) > 1):
    filename = sys.argv[1]

win = FancyFileServer (filename)
win.connect ("delete-event", Gtk.main_quit)
win.show_all ()
Gtk.main ()
