# greaseweazle/tools/util.py
#
# Greaseweazle control script: Utility functions.
#
# Written & released by Keir Fraser <keir.xen@gmail.com>
#
# This is free and unencumbered software released into the public domain.
# See the file COPYING for more details, or visit <http://unlicense.org>.

import argparse, os, sys, serial, struct, time, re, platform
import importlib
import logging
import serial.tools.list_ports
from collections import OrderedDict

from greaseweazle import version
from greaseweazle import error
from greaseweazle import usb as USB


class CmdlineHelpFormatter(argparse.ArgumentDefaultsHelpFormatter,
                           argparse.RawDescriptionHelpFormatter):
    def _get_help_string(self, action):
        help = action.help
        if '%no_default' in help:
            return help.replace('%no_default', '')
        if ('%(default)' in help
            or action.default is None
            or action.default is False
            or action.default is argparse.SUPPRESS):
            return help
        return help + ' (default: %(default)s)'


class ArgumentParser(argparse.ArgumentParser):
    def __init__(self, formatter_class=CmdlineHelpFormatter, *args, **kwargs):
        return super().__init__(formatter_class=formatter_class,
                                *args, **kwargs)

def drive_letter(letter):
    types = {
        'A': (USB.BusType.IBMPC, 0),
        'B': (USB.BusType.IBMPC, 1),
        '0': (USB.BusType.Shugart, 0),
        '1': (USB.BusType.Shugart, 1),
        '2': (USB.BusType.Shugart, 2)
    }
    if not letter.upper() in types:
        raise argparse.ArgumentTypeError("invalid drive letter: '%s'" % letter)
    return types[letter.upper()]

def range_str(l):
    if len(l) == 0:
        return '<none>'
    p, str = None, ''
    for i in l:
        if p is not None and i == p[1]+1:
            p = p[0], i
            continue
        if p is not None:
            str += ('%d,' % p[0]) if p[0] == p[1] else ('%d-%d,' % p)
        p = (i,i)
    if p is not None:
        str += ('%d' % p[0]) if p[0] == p[1] else ('%d-%d' % p)
    return str

class TrackSet:

    class TrackIter:
        """Iterate over a TrackSet in physical <cyl,head> order."""
        def __init__(self, ts):
            l = []
            for c in ts.cyls:
                for h in ts.heads:
                    pc = c*ts.step + ts.h_off[h]
                    l.append((pc, h, c))
            l.sort()
            self.l = iter(l)
        def __next__(self):
            self.physical_cyl, self.head, self.cyl = next(self.l)
            return self
    
    def __init__(self, trackspec):
        self.cyls = list()
        self.heads = list()
        self.h_off = [0]*2
        self.step = 1
        self.trackspec = ''
        self.update_from_trackspec(trackspec)

    def update_from_trackspec(self, trackspec):
        """Update a TrackSet based on a trackspec."""
        self.trackspec += trackspec
        for x in trackspec.split(':'):
            k,v = x.split('=')
            if k == 'c':
                cyls = [False]*100
                for crange in v.split(','):
                    m = re.match('(\d\d?)(-(\d\d?))?$', crange)
                    if m is None: raise ValueError()
                    if m.group(3) is None:
                        s,e = int(m.group(1)), int(m.group(1))
                    else:
                        s,e = int(m.group(1)), int(m.group(3))
                    for c in range(s, e+1):
                        cyls[c] = True
                self.cyls = []
                for c in range(len(cyls)):
                    if cyls[c]: self.cyls.append(c)
            elif k == 'h':
                heads = [False]*2
                for hrange in v.split(','):
                    m = re.match('([01])(-([01]))?$', hrange)
                    if m is None: raise ValueError()
                    if m.group(3) is None:
                        s,e = int(m.group(1)), int(m.group(1))
                    else:
                        s,e = int(m.group(1)), int(m.group(3))
                    for h in range(s, e+1):
                        heads[h] = True
                self.heads = []
                for h in range(len(heads)):
                    if heads[h]: self.heads.append(h)
            elif re.match('h[01].off$', k):
                h = int(re.match('h([01]).off$', k).group(1))
                m = re.match('([+-][\d])$', v)
                if m is None: raise ValueError()
                self.h_off[h] = int(m.group(1))
            elif k == 'step':
                self.step = int(v)
                if self.step <= 0: raise ValueError()
            else:
                raise ValueError()
        
    def __str__(self):
        s = 'c=%s' % range_str(self.cyls)
        s += ':h=%s' % range_str(self.heads)
        for i in range(len(self.h_off)):
            x = self.h_off[i]
            if x != 0:
                s += ':h%d.off=%s%d' % (i, '+' if x >= 0 else '', x)
        if self.step != 1: s += ':step=%d' % self.step
        return s

    def __iter__(self):
        return self.TrackIter(self)

def split_opts(seq):
    """Splits a name from its list of options."""
    parts = seq.split('::')
    name, opts = parts[0], dict()
    for x in map(lambda x: x.split(':'), parts[1:]):
        for y in x:
            try:
                opt, val = y.split('=')
            except ValueError:
                opt, val = y, True
            if opt:
                opts[opt] = val
    return name, opts


image_types = OrderedDict(
    { '.adf': 'ADF',
      '.ads': ('ADS','acorn'),
      '.adm': ('ADM','acorn'),
      '.adl': ('ADL','acorn'),
      '.d81': 'D81',
      '.dsd': ('DSD','acorn'),
      '.dsk': 'EDSK',
      '.hfe': 'HFE',
      '.ima': 'IMG',
      '.img': 'IMG',
      '.ipf': 'IPF',
      '.raw': 'KryoFlux',
      '.scp': 'SCP',
      '.ssd': ('SSD','acorn'),
      '.st' : 'IMG' })

def get_image_class(name):
    if os.path.isdir(name):
        typespec = 'KryoFlux'
    else:
        _, ext = os.path.splitext(name)
        error.check(ext.lower() in image_types,
                    """\
%s: Unrecognised file suffix '%s'
Known suffixes: %s"""
                    % (name, ext, ', '.join(image_types)))
        typespec = image_types[ext.lower()]
    if isinstance(typespec, tuple):
        typename, classname = typespec
    else:
        typename, classname = typespec, typespec.lower()
    mod = importlib.import_module('greaseweazle.image.' + classname)
    return mod.__dict__[typename]


def with_drive_selected(fn, usb, args, *_args, **_kwargs):
    usb.set_bus_type(args.drive[0])
    try:
        usb.drive_select(args.drive[1])
        usb.drive_motor(args.drive[1], _kwargs.pop('motor', True))
        fn(usb, args, *_args, **_kwargs)
    except KeyboardInterrupt:
        print()
        usb.reset()
        raise
    finally:
        usb.drive_motor(args.drive[1], False)
        usb.drive_deselect()


def valid_ser_id(ser_id):
    return ser_id and ser_id.upper().startswith("GW")

def score_port(x, old_port=None):
    score = 0
    if x.manufacturer == "Keir Fraser" and x.product == "Greaseweazle":
        score = 20
    elif x.vid == 0x1209 and x.pid == 0x4d69:
        # Our very own properly-assigned PID. Guaranteed to be us.
        score = 20
    elif x.vid == 0x1209 and x.pid == 0x0001:
        # Our old shared Test PID. It's not guaranteed to be us.
        score = 10
    elif x.vid in (0x2E8A, 0x239A):
        # Something from adafruit or raspberry pi
        score = 5
    if score > 0 and valid_ser_id(x.serial_number):
        # A valid serial id is a good sign unless this is a reopen, and
        # the serials don't match!
        if not old_port or not valid_ser_id(old_port.serial_number):
            score = 20
        elif x.serial_number == old_port.serial_number:
            score = 30
        else:
            score = 0
    if old_port and old_port.location:
        # If this is a reopen, location field must match. A match is not
        # sufficient in itself however, as Windows may supply the same
        # location for multiple USB ports (this may be an interaction with
        # BitDefender). Hence we do not increase the port's score here.
        if not x.location or x.location != old_port.location:
            score = 0
    return score

def find_port(old_port=None):
    best_score, best_port = 0, None
    logging.debug("Found these serial ports:")
    for x in serial.tools.list_ports.comports():
        score = score_port(x, old_port)
        logging.debug("%s (%04X / %04X) with score of %d" % (x, x.vid, x.pid, score))
        if score > best_score:
            best_score, best_port = score, x
    if best_port:
        return best_port.device
    raise serial.SerialException('Cannot find the Greaseweazle device')

def port_info(devname):
    for x in serial.tools.list_ports.comports():
        if x.device == devname:
            return x
    return None

def usb_reopen(usb, is_update):
    mode = { False: 1, True: 0 }
    try:
        usb.switch_fw_mode(mode[is_update])
    except (serial.SerialException, struct.error):
        # Mac and Linux raise SerialException ("... returned no data")
        # Win10 pyserial returns a short read which fails struct.unpack
        pass
    usb.ser.close()
    for i in range(10):
        time.sleep(0.5)
        try:
            devicename = find_port(usb.port_info)
            new_ser = serial.Serial(devicename)
        except serial.SerialException:
            # Device not found
            pass
        else:
            new_usb = USB.Unit(new_ser)
            new_usb.port_info = port_info(devicename)
            new_usb.jumperless_update = usb.jumperless_update
            new_usb.can_mode_switch = usb.can_mode_switch
            return new_usb
    raise serial.SerialException('Could not reopen port after mode switch')


def print_update_instructions(usb):
    print("To perform an Update:")
    if not usb.jumperless_update:
        print(" - Disconnect from USB")
        print(" - Install the Update Jumper at pins %s"
              % ("RXI-TXO" if usb.hw_model != 1 else "DCLK-GND"))
        print(" - Reconnect to USB")
    print(" - Run \"gw update\" to download and install latest firmware")


def usb_mode_check(usb, is_update):

    if usb.update_mode and not is_update:
        if usb.can_mode_switch:
            usb = usb_reopen(usb, is_update)
            if not usb.update_mode:
                return usb
        print("ERROR: Greaseweazle is in Firmware Update Mode")
        print(" - The only available action is \"gw update\"")
        if usb.update_jumpered:
            print(" - For normal operation disconnect from USB and remove "
                  "the Update Jumper at pins %s"
                  % ("RXI-TXO" if usb.hw_model != 1 else "DCLK-GND"))
        else:
            print(" - Main firmware is erased: You *must* perform an update!")
        sys.exit(1)

    if is_update and not usb.update_mode:
        if usb.can_mode_switch:
            usb = usb_reopen(usb, is_update)
            error.check(usb.update_mode, """\
Greaseweazle did not change to Firmware Update Mode as requested.
If the problem persists, install the Update Jumper at pins RXI-TXO.""")
            return usb
        print("ERROR: Greaseweazle is not in Firmware Update Mode")
        print_update_instructions(usb)
        sys.exit(1)

    if not usb.update_mode and usb.update_needed:
        print("ERROR: Greaseweazle firmware v%u.%u is unsupported"
              % (usb.major, usb.minor))
        print_update_instructions(usb)
        sys.exit(1)

    return usb


def usb_open(devicename, is_update=False, mode_check=True):

    if devicename is None:
        devicename = find_port()
    
    usb = USB.Unit(serial.Serial(devicename))
    usb.port_info = port_info(devicename)
    is_win7 = (platform.system() == 'Windows' and platform.release() == '7')
    usb.jumperless_update = ((usb.hw_model, usb.hw_submodel) != (1, 0)
                             and not is_win7)
    usb.can_mode_switch = (usb.jumperless_update
                           and not (usb.update_mode and usb.update_jumpered))

    if mode_check:
        usb = usb_mode_check(usb, is_update)

    return usb
    


# Local variables:
# python-indent: 4
# End:
