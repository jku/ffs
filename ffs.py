#!/usr/bin/env python

import argparse, os, signal, socket, sys, tempfile, traceback
from gi.repository import GObject, Gtk, GLib, GUPnPIgd, Pango, Soup

Status = Soup.KnownStatusCode

class FormInfo:
    NO_INFO = 0
    UPLOAD_FAILURE = 1
    UPLOAD_SUCCESS = 2
    DOWNLOAD_NOT_FOUND = 3
    PREPARING_DOWNLOAD = 4
    DOWNLOAD_FAILURE = 5


class IPState:
    UNKNOWN = 0
    AVAILABLE = 1
    UNAVAILABLE = 2

class ArchiveState:
    FAILED = 0
    PREPARING = 1
    READY = 2
    NA = 3

# Utility function to guess the IP (as a string) where the server can be
# reached from the outside. Quite nasty problem actually.
# Copied from http://www.home.unix-ag.org/simon/woof, GPL 2+
def find_ip ():
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


def get_form (allow_upload, form_info, archive_state, shared_file):
    prefix = """<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01//EN"
"http://www.w3.org/TR/html4/strict.dtd">
<html><head><title>Friendly File Server</title>
<meta http-equiv="content-type" content="text/html; charset=utf-8">
</head><body><h1>Hello, this is a Friendly File Server</h1>"""
    postfix = "</body></html>"

    upload_info_part = "<p><br><p>"
    download_info_part = "<p><br></p>"
    if (form_info == FormInfo.UPLOAD_SUCCESS):
        upload_info_part = "<p>Your file was uploaded succesfully.</p>"
    elif (form_info == FormInfo.UPLOAD_FAILURE):
        upload_info_part = "<p>Your upload failed.</p>"
    elif (form_info == FormInfo.DOWNLOAD_NOT_FOUND):
        download_info_part = "<p>The file you requested does not seem to exist.</p>"
    elif (form_info == FormInfo.DOWNLOAD_FAILURE):
        download_info_part = "<p>The file you requested seems to have disappeared.</p>"

    prepare_info = ""
    if (archive_state == ArchiveState.PREPARING):
        prepare_info = "(archive is being prepared, try again soon)"

    upload_part = ""
    if (allow_upload):
        upload_part = """<h2>You can upload a file</h2>
<form action="/" enctype="multipart/form-data" method="post"><p>
<input type="file" name="file" size="20">
<input type="submit" value="Upload"></p></form>%s""" % upload_info_part

    download_part = "<h2>No downloads are available</h2>" + download_info_part
    if (shared_file and archive_state != ArchiveState.FAILED):
        title = "<h2>A file is available for download</h2>"
        file_line = "<p><a href=\"/1\">%s</a> %s</p>" % (shared_file, prepare_info)
        download_part = title + file_line + download_info_part

    return prefix + upload_part + download_part + postfix


def get_human_readable_bytes (size):
    suffixes = ['B','KB','MB','GB','TB']
    i = 0
    while (size > 1024 or i < 1):
        i += 1
        size = size/1024
    return "%d %s" % (size, suffixes[i])


class Zipper ():

    def __init__ (self):
        if (not GLib.find_program_in_path ("7z")):
            raise Exception

    def on_child_process_exit (self, pid, status, callback):
        should_print = True
        wexitstatus = os.WEXITSTATUS (status)
        if (wexitstatus == 0):
            state = ArchiveState.READY
            should_print = False
        elif (wexitstatus == 1):
            # warning
            state = ArchiveState.READY
        else:
            # error
            state = ArchiveState.FAILED

        if (should_print):
            print ("7z returned %s, printing full output:"
                   % wexitstatus)
            line = self.out_7z.readline ()
            while (line):
                sys.stdout.write(" | " + line)
                line = self.out_7z.readline ()

        GLib.spawn_close_pid (pid)
        self.out_7z = None

        callback (state)


    def create_archive (self, files, callback):
        temp_dir = tempfile.mkdtemp ("", "ffs-")
        if (len (files) == 1):
            archive_name = os.path.join (temp_dir, GLib.path_get_basename (files[0]))
        else:
            archive_name = os.path.join (temp_dir, "archive.zip")

        cmd = ["7z",  "-y", "-tzip", "-bd", "-mx=7", "a", archive_name ]
        flags = GLib.SpawnFlags.SEARCH_PATH | GLib.SpawnFlags.DO_NOT_REAP_CHILD
        result = GLib.spawn_async (cmd + files, [],
                                   GLib.get_current_dir (),
                                   flags, None, None,
                                   False, True, False)
        self.out_7z = GLib.IOChannel (result[2])
        self.out_7z.set_close_on_unref (True)
        GLib.child_watch_add (result[0], self.on_child_process_exit, callback)

        return archive_name


class FriendlyFileServer (Gtk.Window):

    def __init__ (self, files, port, allow_uploads):
        Gtk.Window.__init__ (self, title = "Friendly File Server")
        
        self.config_port = port
        self.allow_upload = allow_uploads
        self.server_header = "friendly-file-server"

        try:
            self.zipper = Zipper ()
        except:
            self.zipper = None

        self.shared_file = None
        self.archive_state = ArchiveState.NA

        self.connect ("delete_event", self.delete_event)

        self.set_default_size (350, 250)

        hbox = Gtk.HBox (spacing = 6)
        hbox.set_border_width (18)
        self.add (hbox)

        vbox = Gtk.VBox (spacing = 12)
        hbox.pack_start (vbox, True, True, 0)

        ip_grid = Gtk.Grid ()
        ip_grid.set_row_spacing (3)
        ip_grid.set_column_spacing (12)
        vbox.pack_start (ip_grid, False, False, 0)

        self.local_info_label = Gtk.Label ("Sharing locally at")
        self.local_info_label.set_alignment (0, 0.5)
        ip_grid.attach (self.local_info_label, 0, 1, 1, 1)

        self.local_ip_label = Gtk.Label ("")
        self.local_ip_label.set_selectable (True)
        self.local_ip_label.set_alignment (0, 0.5)
        ip_grid.attach (self.local_ip_label, 1, 1, 1, 1)

        self.upnp_info_label = Gtk.Label ("Sharing on the internet")
        self.upnp_info_label.set_alignment (0, 0.5)
        ip_grid.attach (self.upnp_info_label, 0, 2, 1, 1)

        self.upnp_ip_label = Gtk.Label ("")
        self.upnp_ip_label.set_selectable (True)
        self.upnp_ip_label.set_visible (False)
        self.local_ip_label.set_alignment (0, 0.5)
        ip_grid.attach (self.upnp_ip_label, 1, 2, 1, 1)

        share_box = Gtk.HBox (spacing = 6)
        vbox.pack_start (share_box, True, False, 0)

        self.share_button = Gtk.Button ()
        self.share_button.connect ("clicked", self.on_button_clicked)
        share_box.pack_start (self.share_button, False, False, 0)

        self.sharing_label = Gtk.Label ("")
        self.sharing_label.set_ellipsize (Pango.EllipsizeMode.END)
        self.sharing_label.set_alignment (0, 0.5)
        share_box.pack_start (self.sharing_label, True, True, 0)

        hbox = Gtk.HBox (spacing = 6)
        vbox.pack_end (hbox, False, False, 6)

        self.upload_label = Gtk.Label ("Allow uploads:\n")
        self.upload_label.set_alignment (0, 0.5)
        hbox.pack_start (self.upload_label, True, True, 0)

        self.upload_switch = Gtk.Switch ()
        self.upload_switch.set_active (self.allow_upload)
        hbox.pack_start (self.upload_switch, False, False, 0)
        self.upload_switch.connect ("notify::active", self.on_upload_switch_notify)

        try:
            self.start_server ()
            if (len (files) > 0):
                self.start_sharing (files)
        except:
            pass

        self.update_ui ()


    def on_upload_switch_notify (self, switch, spec):
        self.allow_upload = self.upload_switch.get_active ()
        self.update_ui ()


    def delete_event (self, widget, event, data = None):
        self.stop_server ()
        return False


    def start_server (self):
        self.local_ip = None
        self.local_port = None
        self.local_ip_state = IPState.UNKNOWN

        self.upnp_ip = None
        self.upnp_port = None
        self.upnp_ip_state = IPState.UNKNOWN

        self.server = None
        self.igd = None

        self.upload_count = 0
        self.upload_bytes = 0
        self.upload_dir = None

        self.server = GObject.new (Soup.Server,
                                   port = self.config_port,
                                   server_header = self.server_header)

        self.local_ip = find_ip ()
        self.local_port = self.server.get_port ()
        self.server.add_handler (None, self.on_soup_request, None)
        print "Server starting, guessed uri http://%s:%d" % (self.local_ip, self.local_port)
        self.server.run_async ()

        # Is URI really available (at least from this machine)?
        self.confirm_uri (self.local_ip, self.local_port, False)

        self.upnp_ip_state = IPState.UNAVAILABLE
        self.igd = GUPnPIgd.SimpleIgd ()
        self.igd.connect ("mapped-external-port", self.on_igd_mapped_port)
        # FAILED: python/GI can't cope with signals with GError
        # self.igd.connect ("error-mapping-port", self.on_igd_error)
        self.igd.add_port ("TCP",
                           self.local_port, # remote port really
                           self.local_ip, self.local_port,
                           0, "Friendly File Server")
        self.upnp_ip_state = IPState.UNKNOWN


    def stop_server (self):
        self.stop_sharing ()

        if (self.igd):
            self.igd.remove_port ("TCP", self.local_port)
            self.igd = None

        if (self.server):
            self.server.disconnect ()
            self.server = None


    def update_ui (self, should_grab = False):
        if (self.server == None):
            self.share_button.set_label ("Share files")
            if (self.config_port == 0):
                self.sharing_label.set_text ("Failed to start the web server.")
            else:
                self.sharing_label.set_text ("Failed to start the web server on port %d."
                                             % self.config_port)
            self.set_sensitive (False)
            return

        # always show the local address
        self.local_ip_label.set_text ("http://%s:%d" % (self.local_ip, self.local_port))

        # only show the port-forwarded opened address if we know it works ...
        if (self.upnp_ip_state == IPState.AVAILABLE):
            self.upnp_ip_label.set_text ("http://%s:%d" % (self.upnp_ip, self.upnp_port))
            self.upnp_ip_label.set_visible (True)
            self.upnp_info_label.set_visible (True)
            if (should_grab):
                self.upnp_ip_label.grab_focus ()
        else:
            self.upnp_ip_label.set_visible (False)
            self.upnp_info_label.set_visible (False)
            if (should_grab):
                self.local_ip_label.grab_focus ()


        if (self.shared_file == None):
            self.share_button.set_label ("Share files")
            if (self.archive_state == ArchiveState.FAILED):
                self.sharing_label.set_text ("Failed to create the archive.")
            else:
                self.sharing_label.set_text ("Currently sharing nothing.")
            return

        self.share_button.set_label ("Stop sharing")

        basename = GLib.path_get_basename (self.shared_file)
        if (self.archive_state == ArchiveState.PREPARING):
            self.sharing_label.set_text ("Now preparing '%s' for sharing"
                                         % basename)
        elif (self.download_count < 1):
            if (self.download_finished_count == 0):
                text = "no downloads yet"
            elif  (self.download_finished_count == 1):
                text = "downloaded once"
            else:
                text = "%d downloads so far" % self.download_finished_count
            self.sharing_label.set_text ("Sharing '%s'\n(%s)"
                                         % (basename, text))
        else:
            if (self.download_finished_count == 0):
                text = "download in progress"
            elif  (self.download_finished_count == 1):
                text = "download in progress, downloaded once already"
            else:
                text = "download in progress, %d downloads so far" \
                       % self.download_finished_count
            self.sharing_label.set_text ("Sharing '%s'\n(%s)"
                                         % (basename, text))

        if (not self.allow_upload and self.upload_count == 0):
            self.upload_label.set_text ("Allow uploads:\n")
        elif (self.upload_count == 0):
            self.upload_label.set_text ("Allow uploads:\n(No uploads yet)")
        elif (self.upload_count == 1):
            self.upload_label.set_markup ("Allow uploads:\n(<a href='file://%s' title='Open containing folder'>One upload</a> so far, %s)"
                                          % (self.upload_dir, get_human_readable_bytes(self.upload_bytes)))
        elif (self.upload_count > 1):
            self.upload_label.set_markup ("Allow uploads:\n(<a href='file://%s' title='Open containing folder'>%d uploads</a> so far, totalling %s)"
                                          % (self.upload_dir, self.upload_count, get_human_readable_bytes(self.upload_bytes)))


    def on_soup_message_wrote_body (self, message):
        self.download_finished_count += 1
        self.download_count -= 1
        self.update_ui ()
        print " * Download finished"


    def on_soup_request (self, server, message, path, query, client, data):
        if (path == "/"):
            if (message.method == "GET" or message.method == "HEAD"):
                self.reply_request (message, Status.OK, FormInfo.NO_INFO)
            elif (message.method == "POST"):
                try:
                    self.handle_upload_request (message)
                except:
                    print "Failed to handle upload request: Internal server error"
                    traceback.print_exc ()
                    self.reply_request (message, Status.INTERNAL_SERVER_ERROR, FormInfo.UPLOAD_FAILURE)
                    return
            else:
                message.set_status (Status.METHOD_NOT_ALLOWED)
        else:
            if (message.method == "GET" or message.method == "HEAD"):
                try:
                    self.handle_download_request (message, path)
                except:
                    print "Failed to handle download request for '%s': Internal server error" % self.shared_file
                    traceback.print_exc ()
                    self.reply_request (message, Status.INTERNAL_SERVER_ERROR, FormInfo.DOWNLOAD_FAILURE)
                    return
            else:
                message.set_status (Status.METHOD_NOT_ALLOWED)


    def reply_request (self, message, status, form_info):
        try:
            basename = GLib.path_get_basename (self.shared_file)
        except:
            basename = None
        form = get_form (self.allow_upload, form_info,
                         self.archive_state, basename)
        message.set_response ("text/html", Soup.MemoryUse.COPY, form)
        message.set_status (status)



    def handle_upload_request (self, message):
        if (not self.allow_upload):
            self.reply_request (message, Status.FORBIDDEN, FormInfo.NO_INFO)
            return

        mp = Soup.Multipart.new_from_message (message.request_headers,
                                              message.request_body)
        [has_part, header, body] = mp.get_part (0)
        if (not has_part):
            self.reply_request (message, Status.BAD_REQUEST, FormInfo.UPLOAD_FAILURE)
            return

        data = body.get_data ()
        [has_cd, cd, params] = header.get_content_disposition ()

        basename = "Upload"
        if (has_cd):
            basename = params["filename"]
        new_filename = self.get_upload_filename (basename)

        with open (new_filename, "w") as f:
            f.write (data)

        self.reply_request (message, Status.OK, FormInfo.UPLOAD_SUCCESS)
        self.upload_count += 1
        self.upload_bytes += len(data)
        self.update_ui ()


    def handle_download_request (self, message, path):
        # could handle multiple files here ...
        if (path != "/1"):
            self.reply_request (message, Status.NOT_FOUND, FormInfo.DOWNLOAD_NOT_FOUND)
            return

        if (message.method == "HEAD"):
            # avoid loading the file just for confirm_url ()
            message.set_status (Status.OK)
            return

        if (self.archive_state == ArchiveState.PREPARING):
            self.reply_request (message, Status.ACCEPTED, FormInfo.PREPARING_DOWNLOAD)
            return

        shared_content = GLib.file_get_contents (self.shared_file)[1]

        message.set_status (Status.OK)
        attachment = {"filename": GLib.path_get_basename (self.shared_file)}
        message.response_headers.set_content_disposition ("attachment", attachment)
        message.response_body.append_buffer (Soup.Buffer.new (shared_content))

        message.connect ("wrote-body", self.on_soup_message_wrote_body)
        self.download_count += 1
        self.update_ui ()


    def on_test_response (self, session, message, is_upnp):
        state = IPState.UNAVAILABLE
        if (message.response_headers.get_one ("server") == self.server_header):
            state = IPState.AVAILABLE

        if (is_upnp):
            self.upnp_ip_state = state
            if (state == IPState.AVAILABLE):
                print ("Port-forward confirmed to work ")
        else:
            self.local_ip_state = state

        self.update_ui ()


    def confirm_uri (self, ip, port, is_upnp):
        uri = Soup.URI ()
        uri.set_scheme ("http")
        uri.set_host (ip)
        uri.set_path ("/")
        uri.set_port (port)

        msg = Soup.Message ()
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
        if(self.upnp_ip_state == IPState.AVAILABLE and
           self.upnp_ip == ext_ip and
           self.upnp_port == ext_port):
            return

        print "Port-forwarded http://%s:%d" % (ext_ip, ext_port)
        self.upnp_ip = ext_ip
        self.upnp_port = ext_port
        self.upnp_ip_state = IPState.UNKNOWN
        self.confirm_uri (ext_ip, ext_port, True)


    def start_sharing (self, files):
        if (self.shared_file != None):
            self.stop_sharing ()

        if (len (files) > 1 or GLib.file_test (files[0], GLib.FileTest.IS_DIR)):
            self.archive_state = ArchiveState.FAILED
            self.shared_file = self.zipper.create_archive (files, self.on_archive_ready)
            self.archive_state = ArchiveState.PREPARING
        elif (len (files) == 1):
            self.archive_state = ArchiveState.NA
            self.shared_file = files[0]

        self.download_count = 0
        self.download_finished_count = 0

        self.update_ui (should_grab = True)


    def stop_sharing (self):
        if (self.archive_state != ArchiveState.NA):
            try:
                os.remove (self.shared_file)
                os.rmdir (GLib.path_get_dirname (self.shared_file))
            except :
                print "Failed to remove temporary file"

        self.shared_file = None

        self.update_ui ()


    def get_upload_filename (self, basename):
        if (not self.upload_dir):
            dl_dir = GLib.get_user_special_dir (GLib.UserDirectory.DIRECTORY_DOWNLOAD)
            dirname = os.path.join (dl_dir, "Friendly File Server Uploads")

            for i in range (2, 1000):
                try:
                    os.makedirs (dirname)
                    self.upload_dir = dirname
                    break
                except os.error:
                    dirname = os.path.join (dl_dir, "Friendly File Server Uploads(%d)" % i)
            if (not self.upload_dir): raise Exception

        fn, ext = os.path.splitext (basename)
        new_filename = os.path.join (self.upload_dir, "%s" % basename)

        if (not os.path.exists (new_filename)):
            return (new_filename)

        for i in range (2, 1000):
            new_filename = os.path.join (self.upload_dir, "{}({}){}".format ( fn, i, ext ))
            if (not os.path.exists (new_filename)):
                return new_filename

        raise Exception


    def on_archive_ready (self, state):
        self.archive_state = state
        if (self.archive_state == ArchiveState.FAILED):
            self.shared_file = None
        self.update_ui ()


    def on_button_clicked (self, widget):
        if (self.shared_file != None):
            self.stop_sharing ()
        else:
            dialog = Gtk.FileChooserDialog ("Select files or folders to share", self,
                                            Gtk.FileChooserAction.OPEN,
                                            (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                                             "Share", Gtk.ResponseType.OK))
            dialog.set_select_multiple (self.zipper)
            if (self.zipper):
                info = Gtk.Label ("You can select multiple files. If you do, they will "
                                  "be added to a zip archive which will then be shared.")
                dialog.set_extra_widget (info)
            if (dialog.run () == Gtk.ResponseType.OK):
                files = dialog.get_filenames ()
                try:
                    self.start_sharing (files)
                except:
                    self.update_ui ()

            dialog.destroy ()


# https://bugzilla.gnome.org/show_bug.cgi?id=622084
signal.signal (signal.SIGINT, signal.SIG_DFL)

parser = argparse.ArgumentParser (description = "Share files on the internet.")
parser.add_argument ("file", nargs = "*", help = "file that should be shared")
parser.add_argument ("-p", "--port", type = int, default = 0)
parser.add_argument ("-u", "--allow-uploads", action = "store_true")
args = parser.parse_args ()

win = FriendlyFileServer (list(set(args.file)), args.port, args.allow_uploads)
win.connect ("delete-event", Gtk.main_quit)
win.show_all ()
Gtk.main ()
