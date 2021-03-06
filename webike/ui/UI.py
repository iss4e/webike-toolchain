import gi

gi.require_version('Gtk', '3.0')

import logging
import threading
from datetime import timedelta, datetime

from dateutil.relativedelta import relativedelta
from gi.repository import Gtk, GLib, GObject
from matplotlib.backends.backend_gtk3cairo import FigureCanvasGTK3Cairo as FigureCanvas
from matplotlib.figure import Figure
from pymysql import MySQLError

from webike.ui.Toolbar import PlotToolbar
from webike.ui.grapher.ChargeGrapher import ChargeGrapher
from webike.ui.grapher.TempGrapher import TempGrapher
from webike.ui.grapher.DensityGrapher import DensityGrapher
from webike.util import DB
from webike.util.DB import DictCursor, Connection
from webike.util.Logging import BraceMessage as __

__author__ = "Niko Fink"
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(threadName)-10.10s %(levelname)-3.3s"
                                                " %(name)-12.12s - %(message)s")

entries = {
    'entryHost': 'host',
    'entryPort': 'port',
    'entryDB': 'db',
    'entryUser': 'user',
    'entryPassword': 'passwd'
}

graphers = {
    "State of Charge": ChargeGrapher,
    "Temperature": TempGrapher,
    "Data Density": DensityGrapher
}


class UI:
    def __init__(self):
        self.connection = None
        self.cursor = None
        self.builder = None
        self.cred = DB.default_credentials()
        self.fig = Figure()

    def __enter__(self):
        self.builder = Gtk.Builder()
        self.builder.add_from_file('webike/ui/glade/timeline.glade')
        self.builder.connect_signals(self)

        model = Gtk.ListStore(str)
        for k, v in sorted(graphers.items()):
            model.append([k])
        combo = self.builder.get_object('grapherCombo')
        combo.set_model(model)
        combo.set_active(0)
        return self

    def __exit__(self, type, value, traceback):
        if self.cursor:
            self.cursor.close()
        if self.connection:
            self.connection.close()
        if self.fig:
            self.fig.clear()

    def show(self):
        for e, c in entries.items():
            self.builder.get_object(e).set_text(str(self.cred[c]))
        self.builder.get_object('entryPort').set_value(int(self.cred['port']))

        self.builder.get_object('connectDialog').show_all()

    def draw_figure(self):
        logger.debug("enter draw_figure")
        imei = self.builder.get_object('imeiCombo').get_active_text()
        grapher_name = self.builder.get_object('grapherCombo').get_active_text()
        callback = lambda i, b, e: GLib.idle_add(self.display_figure, i, b, e)
        grapher = graphers[grapher_name](callback, self.cursor, self.fig)
        year = int(self.builder.get_object('yearButton').get_text())
        month = int(self.builder.get_object('monthButton').get_text())
        begin = datetime(year=year, month=month, day=1)
        end = begin + relativedelta(months=1) - timedelta(seconds=1)

        self.set_processing(True)

        logger.info(__("Plotting {} -- {}-{} from {} to {}", imei, year, month, begin, end))
        threading.Thread(target=grapher, args=(imei, begin, end), daemon=True).start()

    def display_figure(self, imei, begin, end):
        logger.info(__("Finished plotting {} -- {}-{} from {} to {}", imei, end.year, end.month, end, begin))
        self.fig.canvas.draw()
        self.set_processing(False)
        logger.debug("leave display_figure")

    def set_processing(self, processing):
        self.builder.get_object('redrawSpinner').set_visible(processing)
        self.builder.get_object('redrawButton').set_visible(not processing)

        self.builder.get_object('topbarContainer').set_sensitive(not processing)
        self.builder.get_object('toolbarContainer').set_sensitive(not processing)

        self.on_grapher_changed(None)

    ###########################################################################
    # Signals
    ###########################################################################

    def on_window_destroy(self, widget):
        Gtk.main_quit()

    def on_grapher_changed(self, widget):
        grapher_name = self.builder.get_object('grapherCombo').get_active_text()
        requires_month = graphers[grapher_name].requires_month()
        self.builder.get_object('yearButton').set_sensitive(requires_month)
        self.builder.get_object('monthButton').set_sensitive(requires_month)

    def do_redraw(self, widget):
        self.draw_figure()

    def do_previous(self, widget):
        self.builder.get_object('monthButton').spin(Gtk.SpinType.STEP_BACKWARD, 1)
        self.draw_figure()

    def do_next(self, widget):
        self.builder.get_object('monthButton').spin(Gtk.SpinType.STEP_FORWARD, 1)
        self.draw_figure()

    def do_wrap_month(self, widget):
        month = int(widget.get_text())
        if month == 1:
            self.builder.get_object('yearButton').spin(Gtk.SpinType.STEP_FORWARD, 1)
        else:
            self.builder.get_object('yearButton').spin(Gtk.SpinType.STEP_BACKWARD, 1)

    def do_connect(self, widget):
        for e, c in entries.items():
            self.cred[c] = self.builder.get_object(e).get_text()
        self.cred['port'] = int(self.cred['port'])

        try:
            self.connection = Connection(connect_timeout=2, **self.cred)
            self.cursor = self.connection.cursor(DictCursor)
        except MySQLError as e:
            logger.error("Could not connect to MySQL server", exc_info=e)
            label = self.builder.get_object('labelConnMsg')
            # TODO change style
            # attr = Pango.AttrList()
            # attr['foreground'] = "#cccc00"
            # label.set_attributes(attr)
            # label.set_size_request(label.get_allocated_width(), -1)
            # label.set_line_wrap(True)
            # label.set_line_wrap_mode(Pango.WrapMode.WORD)
            label.set_text(str(e))
            return

        connectDialog = self.builder.get_object('connectDialog')
        window = self.builder.get_object('window')

        canvas = FigureCanvas(self.fig)
        self.builder.get_object('plotContainer').add(canvas)
        toolbar = PlotToolbar(canvas, window)
        self.builder.get_object('toolbarContainer').add(toolbar)

        connectDialog.hide()
        window.show_all()
        self.set_processing(False)
        GLib.idle_add(self.draw_figure)


def main():
    GObject.threads_init()
    with UI() as ui:
        ui.show()
        Gtk.main()


if __name__ == "__main__":
    main()
