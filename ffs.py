#!/usr/bin/env python

import argparse, avahi, logging, os, signal, socket, sys, tempfile, traceback, types
from gi.repository import Gio, GLib, GObject, Gtk, GUPnPIgd, Pango, Soup

FFS_APP_NAME = "Friendly File Server"

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


def get_form (allow_upload, form_info, archive_state, shared_file, username):
    if (username):
        app_name = username + "'s " + FFS_APP_NAME
    else:
        app_name = FFS_APP_NAME

    prefix = """<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01//EN"
"http://www.w3.org/TR/html4/strict.dtd">
<html><head><title>%s</title>
<meta http-equiv="content-type" content="text/html; charset=utf-8">
</head><body><h1>Hello, this is %s</h1>""" % (app_name, app_name)
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


class FriendlyZipper ():

    def __init__ (self):
        if (not GLib.find_program_in_path ("7z")):
            raise Exception

    def on_child_process_exit (self, pid, status, callback):
        print_func = None
        wexitstatus = os.WEXITSTATUS (status)
        if (wexitstatus == 0):
            state = ArchiveState.READY
        elif (wexitstatus == 1):
            state = ArchiveState.READY
            print_func = logging.warning
        else:
            state = ArchiveState.FAILED
            print_func = logging.error

        if (print_func):
            print_func ("7z returned %s, printing full output:"
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

class FriendlyZeroconfService:

    def __init__ (self, name, port, stype="_http._tcp",
                  domain="", host="", text="path=/"):

        # these _should_ not block but async would still be proper

        server = Gio.DBusProxy.new_for_bus_sync (Gio.BusType.SYSTEM,
                                                 0,
                                                 None,
                                                 avahi.DBUS_NAME,
                                                 avahi.DBUS_PATH_SERVER,
                                                 avahi.DBUS_INTERFACE_SERVER,
                                                 None)
        self.group = Gio.DBusProxy.new_for_bus_sync (Gio.BusType.SYSTEM,
                                                     0,
                                                     None,
                                                     avahi.DBUS_NAME,
                                                     server.EntryGroupNew(),
                                                     avahi.DBUS_INTERFACE_ENTRY_GROUP,
                                                     None)
        self. group.AddService ("(iiussssqaay)",
                                avahi.IF_UNSPEC, avahi.PROTO_UNSPEC, 0,
                                name, stype, domain, host, port,
                                avahi.string_array_to_txt_array([text]))
        self.group.Commit ("()")

    def shutdown (self):
        self.group.Reset ("()")


class FriendlyFileServer ():

    # just shoot me for this... I want this class to subclass
    # Soup.Server but the fucker aborts in __init__ if the port
    # is already in use. I have to use GObject.new() to get the 
    # exception that should happen in that case. So this is my
    # workaround "subclassing" for now:
    def __getattr__ (self, attr):
        if hasattr (self._obj, attr):
            attr_value = getattr (self._obj, attr)
            if isinstance (attr_value, types.MethodType):
                def callable (*args, **kwargs):
                    return attr_value (*args, **kwargs)
                return callable
            else:
                return attr_value
        else:
            raise AttributeError


    def __init__ (self, port = 0, allow_uploads = False, change_callback = None):

        # This should be a call to Soup.Server.__init__(), see note in __getattr__
        self._obj = GObject.new (Soup.Server,
                                 port = port,
                                 server_header = "friendly-file-server")

        self.allow_upload = allow_uploads
        self.change_callback = change_callback
        self.shared_file = None
        self.archive_state = ArchiveState.NA
        self.igd = None
        try:
            self.zipper = FriendlyZipper ()
        except:
            self.zipper = None

        self.upload_count = 0
        self.upload_bytes = 0
        self.upload_dir = None

        self.local_ip = find_ip ()
        self.local_ip_state = IPState.UNKNOWN

        self.add_handler (None, self.on_soup_request, None)
        print ("Server starting, guessed uri http://%s:%d"
               % (self.local_ip, self.get_port ()))
        self.run_async ()

        # Is URI really available (at least from this machine)?
        self.confirm_uri (self.local_ip, self.get_port(), False)

        self.upnp_ip = None
        self.upnp_port = None
        self.upnp_ip_state = IPState.UNAVAILABLE
        try:
            self.igd = GUPnPIgd.SimpleIgd ()
            self.igd.connect ("mapped-external-port", self.on_igd_mapped_port)
            # FAILED: python/GI can't cope with signals with GError
            # self.igd.connect ("error-mapping-port", self.on_igd_error)
            self.igd.add_port ("TCP",
                            self.get_port (), # remote port really
                               self.local_ip, self.get_port (),
                               0, FFS_APP_NAME)
        except:
            self.igd = None
            self.upnp_ip_state = IPState.UNKNOWN

        try:
            name = GLib.get_real_name () + "'s " + FFS_APP_NAME
            self.zeroconf = FriendlyZeroconfService (name, self.get_port())
        except:
            self.zeroconf = None


    def can_share_multiple (self):
        return (self.zipper != None)


    def shutdown (self):
        self.stop_sharing ()

        if (self.igd):
            self.igd.remove_port ("TCP", self.get_port ())
            self.igd = None

        if (self.zeroconf):
            self.zeroconf.shutdown()
            self.zeroconf = None

        self.disconnect ()


    def on_soup_message_wrote_body (self, message):
        self.download_finished_count += 1
        self.download_count -= 1
        self.change_callback ()


    def on_soup_request (self, server, message, path, query, client, data):
        if (message.method not in  ["POST", "GET", "HEAD"] or
            message.method == "POST" and path != "/"):
            message.set_status (Status.METHOD_NOT_ALLOWED)

        if (message.method == "POST"):
            try:
                self.handle_upload_request (message)
            except:
                logging.error ("Failed to handle upload request: Internal server error")
                traceback.print_exc ()
                self.reply_request (message, Status.INTERNAL_SERVER_ERROR, FormInfo.UPLOAD_FAILURE)
                return
        elif (path == "/" ):
            self.reply_request (message, Status.OK, FormInfo.NO_INFO)
        elif (path == "/favicon.ico"):
            # TODO: need an icon
            message.set_status (Status.NOT_FOUND)
        else:
            try:
                self.handle_download_request (message, path)
            except:
                logging.error ("Failed to handle download request for '%s': Internal server error"
                               % self.shared_file)
                traceback.print_exc ()
                self.reply_request (message, Status.INTERNAL_SERVER_ERROR, FormInfo.DOWNLOAD_FAILURE)
                return


    def reply_request (self, message, status, form_info):
        try:
            basename = GLib.path_get_basename (self.shared_file)
        except:
            basename = None
        form = get_form (self.allow_upload, form_info,
                         self.archive_state, basename,
                         GLib.get_real_name ())
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
        print "Received upload %s" % basename
        self.change_callback ()


    def handle_download_request (self, message, path):
        # could handle multiple files here ...
        if (path != "/1" or not self.shared_file):
            self.reply_request (message, Status.NOT_FOUND, FormInfo.DOWNLOAD_NOT_FOUND)
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
        self.change_callback ()


    def on_test_response (self, session, message, is_upnp):
        state = IPState.UNAVAILABLE
        if (message.response_headers.get_one ("server") == self.get_property ("server-header")):
            state = IPState.AVAILABLE

        if (is_upnp):
            self.upnp_ip_state = state
            if (state == IPState.AVAILABLE):
                print ("Port-forward confirmed to work ")
        else:
            self.local_ip_state = state
        self.change_callback ()


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
        self.upnp_ip_state = IPState.UNAVAILABLE
        self.change_callback ()


    def on_igd_mapped_port (self, igd, proto,
                            ext_ip, old_ext_ip, ext_port,
                            local_ip, local_port,
                            desc):
        if(self.upnp_ip_state == IPState.AVAILABLE and
           self.upnp_ip == ext_ip and
           self.upnp_port == ext_port):
            return

        print ("Port-forwarded http://%s:%d" % (ext_ip, ext_port))
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
        self.change_callback ()


    def stop_sharing (self):
        if (self.archive_state != ArchiveState.NA):
            try:
                os.remove (self.shared_file)
                os.rmdir (GLib.path_get_dirname (self.shared_file))
            except :
                logging.warning ("Failed to remove temporary archive")

        self.shared_file = None
        self.change_callback ()


    def get_upload_filename (self, basename):
        if (not self.upload_dir):
            dl_dir = GLib.get_user_special_dir (GLib.UserDirectory.DIRECTORY_DOWNLOAD)
            dirname = os.path.join (dl_dir, "%s Uploads" % FFS_APP_NAME)

            for i in range (2, 1000):
                try:
                    os.makedirs (dirname)
                    self.upload_dir = dirname
                    break
                except os.error:
                    dirname = os.path.join (dl_dir, "%s Uploads(%d)" % (FFS_APP_NAME, i))
            if (not self.upload_dir): raise Exception

        fn, ext = os.path.splitext (basename)
        new_fn = os.path.join (self.upload_dir, "%s" % basename)

        if (not os.path.exists (new_fn)):
            return (new_fn)

        for i in range (2, 1000):
            new_fn = os.path.join (self.upload_dir,
                                   "{}({}){}".format ( fn, i, ext ))
            if (not os.path.exists (new_fn)):
                return new_fn

        raise Exception


    def on_archive_ready (self, state):
        self.archive_state = state
        if (self.archive_state == ArchiveState.FAILED):
            self.shared_file = None
        self.change_callback ()


class FriendlyWindow (Gtk.Window):

    def __init__ (self, files, port, allow_uploads):
        Gtk.Window.__init__ (self, title = FFS_APP_NAME)

        self.config_port = port

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
        self.upload_switch.set_active (allow_uploads)
        hbox.pack_start (self.upload_switch, False, False, 0)
        self.upload_switch.connect ("notify::active", self.on_upload_switch_notify)

        try:
            self.server = FriendlyFileServer (port, allow_uploads, self.on_server_change)
            if (len (files) > 0):
                self.server.start_sharing (files)
        except:
            self.server = None
            self.update_ui ()


    def on_upload_switch_notify (self, switch, spec):
        self.server.allow_upload = self.upload_switch.get_active ()
        self.update_ui ()


    def delete_event (self, widget, event, data = None):
        if (self.server):
            self.server.shutdown ()
        return False


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
        self.local_ip_label.set_text ("http://%s:%d" % (self.server.local_ip, self.server.get_port ()))

        # only show the port-forwarded opened address if we know it works ...
        if (self.server.upnp_ip_state == IPState.AVAILABLE):
            self.upnp_ip_label.set_text ("http://%s:%d" % (self.server.upnp_ip, self.server.upnp_port))
            self.upnp_ip_label.set_visible (True)
            self.upnp_info_label.set_visible (True)
            if (should_grab):
                self.upnp_ip_label.grab_focus ()
        else:
            self.upnp_ip_label.set_visible (False)
            self.upnp_info_label.set_visible (False)
            if (should_grab):
                self.local_ip_label.grab_focus ()

        if (not self.server.allow_upload and self.server.upload_count == 0):
            self.upload_label.set_text ("Allow uploads:\n")
        elif (self.server.upload_count == 0):
            self.upload_label.set_text ("Allow uploads:\n(No uploads yet)")
        elif (self.server.upload_count == 1):
            self.upload_label.set_markup ("Allow uploads:\n(<a href='file://%s' title='Open containing folder'>One upload</a> so far, %s)"
                                          % (self.server.upload_dir, get_human_readable_bytes(self.server.upload_bytes)))
        elif (self.server.upload_count > 1):
            self.upload_label.set_markup ("Allow uploads:\n(<a href='file://%s' title='Open containing folder'>%d uploads</a> so far, totalling %s)"
                                          % (self.server.upload_dir, self.server.upload_count, get_human_readable_bytes(self.server.upload_bytes)))

        if (self.server.shared_file == None):
            self.share_button.set_label ("Share files")
            if (self.server.archive_state == ArchiveState.FAILED):
                self.sharing_label.set_text ("Failed to create the archive.")
            else:
                self.sharing_label.set_text ("Currently sharing nothing.")
            return

        self.share_button.set_label ("Stop sharing")

        basename = GLib.path_get_basename (self.server.shared_file)
        if (self.server.archive_state == ArchiveState.PREPARING):
            self.sharing_label.set_text ("Now preparing '%s' for sharing"
                                         % basename)
        elif (self.server.download_count < 1):
            if (self.server.download_finished_count == 0):
                text = "no downloads yet"
            elif  (self.server.download_finished_count == 1):
                text = "downloaded once"
            else:
                text = "%d downloads so far" % self.server.download_finished_count
            self.sharing_label.set_text ("Sharing '%s'\n(%s)"
                                         % (basename, text))
        else:
            if (self.server.download_finished_count == 0):
                text = "download in progress"
            elif  (self.server.download_finished_count == 1):
                text = "download in progress, downloaded once already"
            else:
                text = "download in progress, %d downloads so far" \
                       % self.server.download_finished_count
            self.sharing_label.set_text ("Sharing '%s'\n(%s)"
                                         % (basename, text))


    def on_server_change (self):
        self.update_ui()


    def on_button_clicked (self, widget):
        if (self.server.shared_file != None):
            self.server.stop_sharing ()
        else:
            dialog = Gtk.FileChooserDialog ("Select files or folders to share", self,
                                            Gtk.FileChooserAction.OPEN,
                                            (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                                             "Share", Gtk.ResponseType.OK))
            dialog.set_select_multiple (self.server.can_share_multiple ())
            if (self.server.can_share_multiple ()):
                info = Gtk.Label ("You can select multiple files. If you do, they will "
                                  "be added to a zip archive which will then be shared.")
                dialog.set_extra_widget (info)
            if (dialog.run () == Gtk.ResponseType.OK):
                files = dialog.get_filenames ()
                try:
                    self.server.start_sharing (files)
                except:
                    pass

            dialog.destroy ()


# https://bugzilla.gnome.org/show_bug.cgi?id=622084
signal.signal (signal.SIGINT, signal.SIG_DFL)

parser = argparse.ArgumentParser (description = "Share files on the internet.")
parser.add_argument ("file", nargs = "*", help = "file that should be shared")
parser.add_argument ("-p", "--port", type = int, default = 0)
parser.add_argument ("-u", "--allow-uploads", action = "store_true")
args = parser.parse_args ()

win = FriendlyWindow (list(set(args.file)), args.port, args.allow_uploads)
win.connect ("delete-event", Gtk.main_quit)
win.show_all ()
Gtk.main ()
