#!/usr/bin/python

"""Attempts (poorly) to fly a crazyflie 2.0 around in a circle.

Much of this is very heavily based on the example code provided by crazyflie.

This is a standalone script based on the crazyflie python libraries.
"""

import logging
import time
from threading import Thread
import subprocess

import cflib
from cflib.crazyflie import Crazyflie
from cflib.crazyflie.log import LogConfig

logging.basicConfig(level=logging.INFO)

def RunDing():
  subprocess.Popen(['/usr/bin/paplay', '/usr/share/sounds/freedesktop/stereo/complete.oga'])


def ThrustAccAdjust(acc_z_diff, last_acc_z_diff):
  # acc.z of ~1.05 means stable (which should be target_vals), > 1.05 means climbing, < 1.05 means decending.
  thrust_delta = 0
  if acc_z_diff < -0.01:
    thrust_delta += 200
    if last_acc_z_diff < -0.02:
      thrust_delta += 300
    if last_acc_z_diff < -0.06 and acc_z_diff < -0.06:
      thrust_delta += 500
    if last_acc_z_diff < -0.04 and acc_z_diff < -0.04:
      thrust_delta += min(-(last_acc_z_diff + acc_z_diff + 0.04)*10000, 3000)
    if last_acc_z_diff < -0.10 and acc_z_diff < -0.10:
      thrust_delta += 500
  elif acc_z_diff > 0.01:
    thrust_delta -= 200
    if last_acc_z_diff > 0.02:
      thrust_delta -= 300
    if last_acc_z_diff > 0.04 and acc_z_diff > 0.04:
      thrust_delta -= min((last_acc_z_diff + acc_z_diff - 0.04)*10000, 3000)
    if last_acc_z_diff > 0.10 and acc_z_diff > 0.10:
      thrust_delta -= 500
  return thrust_delta

def ThrustGyroAdjust(gyro_z_diff, last_gyro_z_diff):
  # a large negative gyro.z (-45) means we're climbing, a positive value (8) means desending. Target _should_ be 0.
  thrust_delta = 0
  if gyro_z_diff < -2:
    thrust_delta -= 200
    if last_gyro_z_diff < -20:
      thrust_delta -= 200
    if last_gyro_z_diff < -2:
      thrust_delta -= 200
  elif gyro_z_diff > 2:
    thrust_delta += 200
    if last_gyro_z_diff > 20:
      thrust_delta += 200
    if last_gyro_z_diff > 2:
      thrust_delta += 200
  return thrust_delta



class HistData(object):
  def __init__(self, keep_entries=50):
    self.entries = []
    self.keep_entries = keep_entries

  def AddData(self, data):
    self.entries.append(data)
    if len(self.entries) > self.keep_entries:
      self.entries.pop(0)

  def GetLastEntry(self):
    return self.entries[-1]

  def GetAvg(self, lastcnt):
    endidx=len(self.entries)
    startidx=max(0, endidx - lastcnt)
    entries = self.entries[startidx:endidx]
    if not len(entries):
      print 'WARNING: NO DATA'
      return 0
    avg = float(sum(entries))/len(entries)
    variance = 0.0
    for val in entries:
      variance += (val - avg)**2
    print 'Returning avg of %s - %.1f, variance %.1f' % (entries, avg, variance)
    return avg, variance


class FlyInCircle(object):
    """Connect to a crazyflie and fly it around in the circle."""

    def __init__(self, link_uri):
        """ Initialize and run with the specified link_uri """

        self._cf = Crazyflie(ro_cache='ramp.ro.json', rw_cache='ramp.rw.json')
        self.stats = {}
        self.logcnt = {}

        self._cf.connected.add_callback(self._connected)
        self._cf.disconnected.add_callback(self._disconnected)
        self._cf.connection_failed.add_callback(self._connection_failed)
        self._cf.connection_lost.add_callback(self._connection_lost)

        self._lg_stab = LogConfig(name='baro', period_in_ms=10)
        self._lg_stab.add_variable('stabilizer.roll', 'float')
        # started off using baro.asl, but it doesn't seem to give great
        # data...
        self._lg_stab.add_variable('baro.asl', 'float')
        # and gyro is useful, but not as useful when it turns.
        self._lg_stab.add_variable('gyro.x', 'float')
        self._lg_stab.add_variable('gyro.y', 'float')
        self._lg_stab.add_variable('gyro.z', 'float')

        # putting these in the other LogConfig resulted in too large of
        # a log packet.  So creating a new one.
        self._lg_stab2 = LogConfig(name='acc', period_in_ms=10)
        self._lg_stab2.add_variable('acc.x', 'float')
        self._lg_stab2.add_variable('acc.y', 'float')
        self._lg_stab2.add_variable('acc.z', 'float')

        self._cf.open_link(link_uri)

        print('Connecting to %s' % link_uri)

    def _connected(self, link_uri):
        """ This callback is called form the Crazyflie API when a Crazyflie
        has been connected and the TOCs have been downloaded."""
        print 'connected...'

        # Adding the configuration cannot be done until a Crazyflie is
        # connected, since we need to check that the variables we
        # would like to log are in the TOC.
        print 'Added log config...'
        try:
            self._cf.log.add_config(self._lg_stab)
            self._cf.log.add_config(self._lg_stab2)
            # This callback will receive the data
            self._lg_stab.data_received_cb.add_callback(self._stab_log_data)
            # This callback will be called on errors
            self._lg_stab.error_cb.add_callback(self._stab_log_error)

            self._lg_stab2.data_received_cb.add_callback(self._stab_log_data)
            # This callback will be called on errors
            self._lg_stab2.error_cb.add_callback(self._stab_log_error)
            # Start the logging
            print 'Starting logging...'
            self._lg_stab.start()
            self._lg_stab2.start()
            print 'Logging done...'
        except KeyError as e:
            print('Could not start log configuration,'
                  '{} not found in TOC'.format(str(e)))
        except AttributeError:
            print('Could not add log config, bad configuration.')

        print 'Starting motors...'
        # Start a separate thread to do the motor control.
        # Do not hijack the calling thread!
        Thread(target=self._fly).start()

    def _stab_log_error(self, logconf, msg):
        """Callback from the log API when an error occurs"""
        print('Error when logging %s: %s' % (logconf.name, msg))

    def _stab_log_data(self, timestamp, data, logconf):
        """Callback froma the log API when data arrives"""
        if logconf.name not in self.logcnt:
          self.logcnt[logconf.name] = 0
        self.logcnt[logconf.name] += 1
        for val in data:
          if val not in self.stats:
            self.stats[val] = HistData()
          self.stats[val].AddData(data[val])

        if (self.logcnt[logconf.name]%2 == 0):
          buf = []
          for val in sorted(data):
            buf.append('%s: %.2f' % (val, data[val]))
          print('[%d][%s]: %s' % (timestamp, logconf.name, ', '.join(buf)))

    def _connection_failed(self, link_uri, msg):
        """Callback when connection initial connection fails (i.e no Crazyflie
        at the specified address)"""
        print('Connection to %s failed: %s' % (link_uri, msg))

    def _connection_lost(self, link_uri, msg):
        """Callback when disconnected after a connection has been made (i.e
        Crazyflie moves out of range)"""
        print('Connection to %s lost: %s' % (link_uri, msg))

    def _disconnected(self, link_uri):
        """Callback when the Crazyflie is disconnected (called in all cases)"""
        print('Disconnected from %s' % link_uri)

    def _fly(self):
        def _RunThrust(thrust):
            print 'roll: %.2f, pitch: %.2f, yawrate: %d, thrust: %d' % (roll, pitch, yawrate, thrust)
            self._cf.commander.send_setpoint(roll, pitch, yawrate, thrust)
            time.sleep(0.1)

        thrust_mult = 1
        thrust_step = 500
        thrust = 28000
        pitch = 0.70
        #roll = 0.93
        roll = -0.10
        yawrate = 0
        initial_pitch = pitch
        initial_roll = roll

        time.sleep(2)
        self.target_vals = {}

        for tgt in ('gyro.z', 'acc.z', 'baro.asl'):
          self.target_vals[tgt] = self.stats[tgt].GetAvg(5)[0]
          while not self.target_vals[tgt]:
            time.sleep(2)
            self.target_vals[tgt] = self.stats[tgt].GetAvg(5)[0]

        print '**** Setpoints: %s' % self.target_vals

        # Unlock startup thrust protection
        self._cf.commander.send_setpoint(0, 0, 0, 0)

        print '**** Starting takeoff'
        for thrust in xrange(31500, 37500, 500):
            _RunThrust(thrust)

        thrust = 34500
        _RunThrust(thrust)

        RunDing()
        print '**** Starting hover'
        last_gyro_z_diff = 0.0
        last_acc_z_diff = 0.0

        yawrate = 100
        pitch += 6.0
        roll += 6.0
        # adjust for the new pitch ... this is very unscientific
        self.target_vals['acc.z'] += 0.01
        for wait in xrange(60):
            acc_z_diff = self.stats['acc.z'].GetLastEntry() - self.target_vals['acc.z']
            thrust_delta = ThrustAccAdjust(acc_z_diff, last_acc_z_diff)
            last_acc_z_diff = acc_z_diff

            # ignoring this for now, since when it is flying in circles the gyro seems to be less reliable.
            #if thrust_delta:
            #  gyro_z_diff = self.stats['gyro.z'].GetLastEntry() - self.target_vals['gyro.z']
            #  thrust += ThrustGyroAdjust(gyro_z_diff, last_gyro_z_diff)
            #  last_gyro_z_diff = gyro_z_diff
            if thrust > 35000 and thrust_delta > 0:
              thrust_delta /= 2
            elif thrust < 30000 and thrust_delta < 0:
              thrust_delta /= 2
            thrust += thrust_delta


            # for safety.  If it gets here, then assume something is wrong and stop.
            if thrust > 43000:
              print '**** ABORT, THRUST TOO HIGH (%d)' % thrust
              break
            if thrust < 20000:
              print '**** ABORT, THRUST TOO LOW (%d)' % thrust
              break

            yawrate = 100
            _RunThrust(thrust)

        RunDing()
        print '**** Starting landing'
        yawrate = 0
        pitch = initial_pitch
        roll = initial_roll
        landingthrust = int(min(35000, thrust - 1000))
        for thrust in xrange(landingthrust, landingthrust-8000, -500):
            _RunThrust(thrust)

        print '**** Ending landing'
        for wait in xrange(10):
            gyro_z_avg, gyro_z_variance = self.stats['gyro.z'].GetAvg(2)
            gyro_z_diff = gyro_z_avg - self.target_vals['gyro.z']
            
            if gyro_z_variance < 2 and (gyro_z_diff > -2 and gyro_z_diff < 2):
              print '*** We may have already landed, aborting this'
              break
            _RunThrust(thrust)


        self._cf.commander.send_setpoint(0, 0, 0, 0)
        # Make sure that the last packet leaves before the link is closed
        # since the message queue is not flushed before closing
        time.sleep(0.1)
        self._cf.close_link()
        time.sleep(0.2)


if __name__ == '__main__':
    # Initialize the low-level drivers (don't list the debug drivers)
    cflib.crtp.init_drivers(enable_debug_driver=False)
    # Scan for Crazyflies and use the first one found
    #print('Scanning interfaces for Crazyflies...')
    #available = cflib.crtp.scan_interfaces()
    #print('Crazyflies found:')
    #for i in available:
    #    print(i[0])
    # always just connect to this and skip the scan.
    connectto = 'radio://0/80/250K'

    le = FlyInCircle(connectto) # available[0][0])
    #if len(available) > 0:
    #else:
    #    print('No Crazyflies found, cannot run example')
