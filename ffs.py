#!/usr/bin/env python

import os, signal, socket, sys, tempfile
from gi.repository import GObject, Gtk, GLib, GUPnPIgd, Pango, Soup


class FancyFileServer (Gtk.Window):
    def __init__ (self, files):
        Gtk.Window.__init__ (self, title="Fancy File Server")
        
        self.port = 55555;
        self.server_header = "fancy-file-server"
        self.have_7z = GLib.find_program_in_path ("7z")
        self.igd = None
        self.server = None
        self.shared_file = None
        self.shared_content = None
        self.shared_file_is_temporary = False
        self.shared_file_state = ""
        self.local_ip = None
        self.request_count = 0
        self.request_finished_count = 0

        self.connect("delete_event", self.delete_event)

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
        self.update_ui ()

        if (len (files) > 0):
            self.start_sharing (files)


    def delete_event(self, widget, event, data=None):
        self.stop_sharing ();
        return False


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

        if (self.shared_file_state == "broken"):
            self.sharing_label.set_text ("Failed to share '{}', sorry.".format (GLib.path_get_basename (self.shared_file)))
            self.address_label.set_text ("")
            self.info_label.set_text ("")
            return

        if (self.shared_file_state == "preparing"):
            self.sharing_label.set_text ("Now preparing '{}' for sharing at".format (GLib.path_get_basename (self.shared_file)))
        else:
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
                # this is for confirm_uri()
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
        session.queue_message (msg, self.on_test_response, None)


    def on_igd_mapped_port (self, igd, proto,
                            ext_ip, old_ext_ip,
                            ext_port, local_ip,
                            desc, data):
        print "NAT punched at http://{}:{}".format (ext_ip, ext_port)
        self.confirm_uri (ext_ip, ext_port)

    def start_sharing (self, files):
        if (len (files) == 0):
            self.shared_file_state = "broken"
            return

        if (len (files) > 1 or GLib.file_test (files[0], GLib.FileTest.IS_DIR)):
            self.shared_file_is_temporary = True
            self.shared_file_state = "preparing"
            self.shared_file = self.create_temporary_archive (files)
        else:
            self.shared_file_is_temporary = False
            self.shared_file_state = "ready"
            self.shared_file = files[0]

        if (self.shared_file == None):
            self.shared_file_state = "broken"
            return

        self.shared_content = None

        self.local_ip = self.find_ip ()
        self.request_count = 0
        self.request_finished_count = 0

        self.server = GObject.new (Soup.Server,
                                   port = self.port,
                                   server_header = self.server_header)
        if (self.server == None):
            # TODO: error?
            return

        self.server.add_handler (None, self.on_soup_request, None)
        self.server.connect ("request-finished", self.on_soup_request_finished)
        print "Server starting, guessed uri http://{}:{}".format(self.local_ip, self.server.get_port ())
        self.server.run_async ()


        # Make sure the URI is really available (at least from this
        # machine).
        self.confirm_uri (self.local_ip, self.server.get_port ())

        self.update_ui ()

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
            try:
                os.remove (self.shared_file);
                os.rmdir (GLib.path_get_dirname (self.shared_file))
            except :
                print "Failed to remove temporary file"

        self.shared_file = None
        self.shared_content = None
        self.shared_file_state = ""

        if (self.igd):
            self.igd.remove_port ("TCP", self.server.get_port ())
            self.igd = None

        if (self.server):
            self.server.disconnect()
            self.server = None

        self.update_ui ()


    def on_child_process_exit (self, pid, status):
        GLib.spawn_close_pid (pid)
        wexitstatus = os.WEXITSTATUS (status)
        if (wexitstatus == 0):
            self.shared_file_state = "ready"
        elif (wexitstatus == 1):
            self.shared_file_state = "ready"
            print ("7z returned 1 (warning), but created the archive.")
        else:
            self.shared_file_state = "broken"
            print ( "oops, 7z returned {}".format (wexitstatus))

        self.update_ui ()


    def create_temporary_archive (self, files):
        temp_dir = tempfile.mkdtemp ("", "ffs-")
        if (len (files) == 1):
            archive_name = "{}/{}.zip".format (temp_dir, GLib.path_get_basename (files[0]))
        else:
            archive_name = "{}/archive.zip".format (temp_dir)

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
            print "Failed to spawn 7z: {}".format (e.message)
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
                self.start_sharing (files)

            dialog.destroy()


# https://bugzilla.gnome.org/show_bug.cgi?id=622084
signal.signal(signal.SIGINT, signal.SIG_DFL)

win = FancyFileServer (sys.argv[1:])
win.connect ("delete-event", Gtk.main_quit)
win.show_all ()
Gtk.main ()
