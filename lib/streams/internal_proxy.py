"""
MIT License

Copyright (C) 2021 ROCKY4546
https://github.com/rocky4546

This file is part of Cabernet

Permission is hereby granted, free of charge, to any person obtaining a copy of this software
and associated documentation files (the "Software"), to deal in the Software without restriction,
including without limitation the rights to use, copy, modify, merge, publish, distribute,
sublicense, and/or sell copies of the Software, and to permit persons to whom the Software
is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or
substantial portions of the Software.
"""

import datetime
import errno
import http
import re
import urllib.request
import time
from collections import OrderedDict
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
from multiprocessing import Queue, Process
from threading import Thread

import lib.m3u8 as m3u8
import lib.streams.m3u8_queue as m3u8_queue
from lib.streams.video import Video
from lib.streams.atsc import ATSCMsg
from lib.db.db_config_defn import DBConfigDefn
from lib.db.db_channels import DBChannels
from .stream import Stream


class InternalProxy(Stream):

    def __init__(self, _plugins, _hdhr_queue):
        self.last_refresh = None
        self.channel_dict = None
        self.write_buffer = None
        self.file_filter = None
        self.duration = 6
        self.last_ts_index = -1
        super().__init__(_plugins, _hdhr_queue)
        self.config = self.plugins.config_obj.data
        self.db_configdefn = DBConfigDefn(self.config)
        self.db_channels = DBChannels(self.config)
        self.video = Video(self.config)
        self.atsc = ATSCMsg()
        self.initialized_psi = False
        self.in_queue = Queue()
        self.out_queue = Queue(maxsize=3)

    def stream(self, _channel_dict, _write_buffer):
        """
        Processes m3u8 interface without using ffmpeg
        """
        self.config = self.db_configdefn.get_config()
        self.channel_dict = _channel_dict
        self.write_buffer = _write_buffer
        duration = 6
        t_m3u8 = Process(target=m3u8_queue.start, args=(
            self.config, self.in_queue, self.out_queue, _channel_dict,))
        t_m3u8.start()
        play_queue_dict = OrderedDict()
        self.last_refresh = time.time()
        stream_uri = self.get_stream_uri(_channel_dict)
        if not stream_uri:
            self.logger.warning('Unknown Channel')
            return
        self.logger.debug('M3U8: {}'.format(stream_uri))
        self.file_filter = None
        if self.config[_channel_dict['namespace'].lower()]['player-enable_url_filter']:
            stream_filter = self.config[_channel_dict['namespace'].lower()]['player-url_filter']
            if stream_filter is not None:
                self.file_filter = re.compile(stream_filter)
            else:
                self.logger.warning('[{}]][player-enable_url_filter]'
                    ' enabled but [player-url_filter] not set'
                    .format(_channel_dict['namespace'].lower()))

        while True:
            try:
                added = 0
                removed = 0
                i = 3
                while i > 0:
                    try:
                        playlist = m3u8.load(stream_uri)
                        break
                    except urllib.error.HTTPError as e:
                        self.logger.info('M3U8 Exception caught, retrying: {}'.format(e))
                        time.sleep(1.5)
                        i -= 1
                if i < 1:
                    break
                removed += self.remove_from_stream_queue(playlist, play_queue_dict)
                added += self.add_to_stream_queue(playlist, play_queue_dict)
                if added == 0 and duration > 0:
                    time.sleep(duration * 0.3)
                elif self.plugins.plugins[_channel_dict['namespace']].plugin_obj \
                        .is_time_to_refresh_ext(self.last_refresh, _channel_dict['instance']):
                    stream_uri = self.get_stream_uri(_channel_dict)
                    self.logger.debug('M3U8: {}'.format(stream_uri))
                    self.last_refresh = time.time()
                self.play_queue(play_queue_dict)
            except IOError as e:
                # Check we hit a broken pipe when trying to write back to the client
                if e.errno in [errno.EPIPE, errno.ECONNABORTED, errno.ECONNRESET, errno.ECONNREFUSED]:
                    # Normal process.  Client request end of stream
                    self.logger.info('2. Connection dropped by end device {}'.format(e))
                    break
                else:
                    self.logger.error('{}{}'.format(
                        '3 UNEXPECTED EXCEPTION=', e))
                    raise
        
        t_m3u8.terminate()
        t_m3u8.join()
        del t_m3u8
        self.clear_queues()

    def clear_queues(self):
        while not self.in_queue.empty():
            x = self.in_queue.get()
        while not self.out_queue.empty():
            x = self.out_queue.get()
    

    def add_to_stream_queue(self, _playlist, _play_queue_dict):
        total_added = 0
        if _playlist.keys != [None]:
            keys = [{"uri": key.absolute_uri, "method": key.method, "iv": key.iv} \
                for key in _playlist.keys if key]
        else:
            keys = [None for i in range(0, len(_playlist.segments))]
            
        for m3u8_segment, key in zip(_playlist.segments, keys):
            uri = m3u8_segment.absolute_uri
            if uri not in _play_queue_dict.keys():
                played = False
                filtered = False
                if self.file_filter is not None:
                    m = self.file_filter.match(uri)
                    if m:
                        filtered = True
                _play_queue_dict[uri] = {
                    'uid': self.channel_dict['uid'],
                    'played': played,
                    'filtered': filtered,
                    'duration': m3u8_segment.duration,
                    'key': key
                }
                self.logger.debug(f"Added {uri} to play queue")
                total_added += 1
                if not played:
                    self.in_queue.put({'uri': uri, 
                        'data': _play_queue_dict[uri]})
                    time.sleep(0.2)
        return total_added

    def remove_from_stream_queue(self, _playlist, _play_queue_dict):
        total_removed = 0
        for segment_key in list(_play_queue_dict.keys()):
            is_found = False
            for segment_m3u8 in _playlist.segments:
                uri = segment_m3u8.absolute_uri
                if segment_key == uri:
                    is_found = True
                    break
            if not is_found:
                if _play_queue_dict[segment_key]['played']:
                    del _play_queue_dict[segment_key]
                    total_removed += 1
                    self.logger.debug(f"Removed {segment_key} from play queue")
                continue
            else:
                break
        return total_removed

    def play_queue(self, _play_queue_dict):
        while not self.out_queue.empty():
            out_queue_item = self.out_queue.get()
            uri = out_queue_item['uri']
            data = _play_queue_dict[uri]
            if data['filtered']:
                self.write_buffer.write(out_queue_item['data'])
                time.sleep(0.3 * self.duration)
                data['played'] = True
            else:
                self.video.data = out_queue_item['data']
                chunk_updated = self.atsc.update_sdt_names(self.video.data[:80], 
                    self.channel_dict['namespace'].encode(),
                    self.set_service_name(self.channel_dict).encode())
                self.video.data = chunk_updated + self.video.data[80:]
                self.duration = data['duration']
                wait = 0.3 * self.duration
                self.check_ts_counter(uri)
                self.logger.info(f"Serving {uri} ({self.duration}s) ({len(self.video.data)}B)")
                data['played'] = True
                self.write_buffer.write(self.video.data)
                if wait > 0:
                    time.sleep(wait)
        self.video.terminate()

    def get_ts_counter(self, _uri):
        r = re.compile( r'(\d+)\.ts' )
        m = r.findall(_uri)
        if len(m) == 0:
            return 0
        else:
            return int(m[len(m)-1])

    def check_ts_counter(self, _uri):
        ts_counter = self.get_ts_counter(_uri)
        if ts_counter-1 != self.last_ts_index:
            if self.last_ts_index != -1:
                self.logger.info('TC Counter Discontinuity {} vs {} next uri: {}' \
                    .format(self.last_ts_index, ts_counter, _uri))
        self.last_ts_index = ts_counter


