from SocketIOTQDM import SocketIOTQDM
from debug_print import debug_print
from flask import request 


import pytz
import requests
import socketio
import socketio.exceptions
import yaml
from flask import jsonify, send_from_directory
from zeroconf import ServiceBrowser, ServiceStateChange, Zeroconf
from flask_socketio import SocketIO

import json
import os
import queue
import shutil
import socket
import sys
import time
from datetime import datetime
from threading import Thread
from typing import cast

from utils import get_source_by_mac_address, getDateFromFilename, getMetaData
from utils import compute_md5


class Device:
    def __init__(self, filename: str, local_dashboard_sio:SocketIO) -> None:
        """
        Initialize the Device object with a configuration file.

        Args:
            filename (str): The path to the configuration file.
        """
        ## the device dashboard socket.  for showing connection status
        ## and echo console messages.  
        self.m_local_dashboard_sio = local_dashboard_sio

        self.m_config = None
        with open(filename, "r") as f:
            self.m_config = yaml.safe_load(f)

        self.m_config["source"] = get_source_by_mac_address()
        debug_print(f"Setting source name to {self.m_config['source']}")

        self.m_config["servers"] = self.m_config.get("servers", [])
        self.m_sio = socketio.Client(
            reconnection=True,
            reconnection_attempts=0,  # Infinite attempts
            reconnection_delay=1,  # Start with 1 second delay
            reconnection_delay_max=5,  # Maximum 5 seconds delay
            randomization_factor=0.5,  # Randomize delays by +/- 50%
            logger=False,  # Enable logging for debugging
            engineio_logger=False  # Enable Engine.IO logging
        )

        self.m_server = None

        self.m_signal = None
        self.m_fs_info = {}

        self.m_files = None

        self.m_md5 = {}
        self.m_updates = {}
        self.m_computeMD5 = self.m_config.get("computeMD5", True)
        self.m_chunk_size = self.m_config.get("chunk_size", 8192)

        self.m_local_tz = self.m_config.get("local_tz", "America/New_York")

        # test to make sure time zone is set correctly. 
        try:
            pytz.timezone(self.m_local_tz)
        except pytz.UnknownTimeZoneError:
            debug_print(f"Invalid config option 'local_tz'. The string '{self.m_local_tz}' is not a valid time zone ")
            sys.exit(1)

        services = ['_http._tcp.local.']
        self.m_zeroconfig = Zeroconf()
        self.m_zero_conf_name = "Airlab_storage._http._tcp.local."
        self.browser = ServiceBrowser(self.m_zeroconfig, services, handlers=[self.on_change])

    def on_local_dashboard_connect(self):
        debug_print("Dashboard connected")
        if self.m_sio.connected:
            self.m_local_dashboard_sio.emit("server_connect",  {"name": self.m_server})
        pass  

    def on_local_dashboard_disconnect(self):
        debug_print("Dashboard disconnected")
        pass

    def on_change(self, zeroconf: Zeroconf, service_type: str, name: str, state_change: ServiceStateChange) -> None:
        if name != self.m_zero_conf_name:
            return

        if state_change is ServiceStateChange.Added:
            info = zeroconf.get_service_info(service_type, name)

            if info:
                addresses = [
                    "%s:%d" % (addr, cast(int, info.port))
                    for addr in info.parsed_scoped_addresses()
                ]

            for address in addresses:
                if address in self.m_config["servers"]:
                    continue
                self.m_config["zero_conf"] =self.m_config.get("zero_conf", [])
                self.m_config["zero_conf"].append(address)

    def _find_server(self):
        servers = []
        servers.extend(self.m_config["servers"])
        servers.extend(self.m_config.get("zero_conf", []))

        for server_full in servers:
            try:
                self.m_server = None
                server, port = server_full.split(":")
                port = int(port)
                debug_print(f"Testing {server}:{port}")
                socket.create_connection((server, port))
                debug_print(f"Connected to {server}:{port}")


                if self.m_sio.connected:
                    debug_print("Clearing prior socket")
                    self.m_sio.disconnect()


                api_key_token = self.m_config["API_KEY_TOKEN"]
                headers = {"X-Api-Key": api_key_token }

                self.m_sio.connect(f"http://{server}:{port}/socket.io", headers=headers, transports=['websocket'])
                self.m_sio.on('control_msg')(self._on_control_msg)
                self.m_sio.on('update_entry')(self._on_update_entry)
                self.m_sio.on('set_project')(self._on_set_project)
                self.m_sio.on('set_md5')(self._on_set_md5)
                self.m_sio.on("device_scan")(self._on_device_scan)
                self.m_sio.on("device_send")(self._on_device_send)
                self.m_sio.on("device_remove")(self.on_device_remove)
                self.m_sio.on("keep_alive_ack")(self._on_keep_alive_ack)
                self.m_sio.on("disconnect")(self._on_disconnect)

                self.m_sio.emit('join', { 'room': self.m_config["source"], "type": "device" })
                self.m_server = server_full
                
                self.m_local_dashboard_sio.emit("server_connect",  {"name": server_full})

                return server_full
            except ConnectionRefusedError as e:
                debug_print(f"Connection Refused: {e}")
                pass

            except OSError as e:
                debug_print(f"OS Error: {e}")
                pass

            except socketio.exceptions.ConnectionError as e:
                debug_print(f"SocketIO Connection error {e}")
                pass

            except ValueError:
                self.m_sio.disconnect()
                pass

            except Exception as e:
                debug_print(e)
                # raise e
                pass
        return None


    def _on_disconnect(self):
        debug_print(f"Got disconnected")
        self.m_local_dashboard_sio.emit("server_disconnect")


    def _on_keep_alive_ack(self):
        pass

    def _on_control_msg(self, data):
        debug_print(data)
        if data.get("action", "") == "cancel":
            self.m_signal = "cancel"

    def _on_update_entry(self, data):
        source = data.get("source")
        if source != self.m_config["source"]:
            return

        relpath = data.get("relpath")
        basename = data.get("basename")
        filename = os.path.join(relpath, basename)
        update = data.get("update")

        self.m_updates[filename] = self.m_updates.get(filename, {})
        self.m_updates[filename].update( update )


    def _on_set_project(self, data):
        debug_print(data)
        source = data.get("source")
        if source != self.m_config["source"]:
            return

        project = data.get("project")
        self.m_config["project"] = project

        self.emitFiles()

    def _on_set_md5(self, data):
        debug_print(data)
        source = data.get("source")
        if source != self.m_config["source"]:
            return

        self.m_computeMD5 = data.get("value", False)



    def _include(self, filename: str) -> bool:
        """
        Check if a file should be included based on its suffix.

        This method checks the file's suffix against the include and exclude suffix lists in the configuration.
        If an include suffix list is present, the file must match one of the suffixes to be included.
        If an exclude suffix list is present, the file will be excluded if it matches any of the suffixes.

        Args:
            filename (str): The name of the file to check.

        Returns:
            bool: True if the file should be included, False otherwise.
        """
        if filename.startswith("."):
            return False

        if "include_suffix" in self.m_config:
            for suffix in self.m_config["include_suffix"]:
                if filename.endswith(suffix):
                    return True
            return False
        if "exclude_suffix" in self.m_config:
            for suffix in self.m_config["exclude_suffix"]:
                if filename.endswith(suffix):
                    return False
            return True

    def _remove_dirpath(self, filename:str):
        for dirroot in self.m_config["watch"]:
            if filename.startswith(dirroot):
                rtn = filename.replace(dirroot, "")
                return rtn.strip("/")
        return filename

    def _scan(self):
        debug_print("Scanning for files")
        self.m_sio.emit("device_status", {"source": self.m_config["source"], "msg": "Scanning for files"})
        self.m_fs_info = {}
        entries = []
        total_size = 0
        for dirroot in self.m_config["watch"]:
            debug_print("Scanning " + dirroot)

            self.m_sio.emit("device_status", {"source": self.m_config["source"], "msg": f"Scanning {dirroot} for files"})

            if os.path.exists(dirroot):
                dev = os.stat(dirroot).st_dev
                if not dev in self.m_fs_info:
                    total, used, free = shutil.disk_usage(dirroot)
                    free_percentage = (free / total) * 100
                    self.m_fs_info[dev] = (dirroot, f"{free_percentage:0.2f}")

            filenames = []
            for root, _, files in os.walk(dirroot):
                for file in files:
                    if not self._include(file):
                        continue
                    filename = os.path.join(root, file).replace(dirroot, "")
                    filename = filename.strip("/")
                    fullpath = os.path.join(root, file)
                    filenames.append((dirroot, filename, fullpath))

        entries, total_size = self._get_metadata(filenames)

        rtn = self._do_md5sum(entries, total_size)

        debug_print("scan complete")
        return rtn

    def _do_md5sum(self, entries, total_size):
        with SocketIOTQDM(total=total_size, desc="Compute MD5 sum", position=0, unit="B", unit_scale=True, leave=False, source=self.m_config["source"], socket=self.m_sio, event="device_status_tqdm") as main_pbar:
            file_queue = queue.Queue()
            rtn_queue = queue.Queue()
            for entry in entries:
                fullpath = os.path.join(entry["dirroot"], entry["filename"])

                last_modified = os.path.getmtime(fullpath)
                do_compute = False
                if fullpath not in self.m_md5:
                    do_compute = True
                elif self.m_md5[fullpath]["last_modified"] > last_modified:
                    do_compute = True

                if do_compute:
                    file_queue.put((fullpath, entry))

            def worker(position:int):
                while True:
                    try:
                        fullpath, entry = file_queue.get(block=False)
                    except queue.Empty:
                        break
                    except ValueError:
                        break

                    if self.m_computeMD5:
                        md5 = compute_md5(fullpath, self.m_chunk_size, 1+position, socket=self.m_sio, source=self.m_config["source"], main_pbar=main_pbar)
                    else:
                        md5 = "0"
                    entry["md5"] = md5
                    rtn_queue.put(entry)

            threads = []
            num_threads = min(self.m_config["threads"], len(entries))
            for i in range(num_threads):
                thread = Thread(target=worker, args=(i,))
                thread.start()
                threads.append(thread)

            for thread in threads:
                thread.join()

        rtn = []
        try:
            while not rtn_queue.empty():
                rtn.append(rtn_queue.get())
        except ValueError:
            pass

        try:
            self.m_sio.emit("device_status", {"source": self.m_config["source"]})
        except socketio.exceptions.BadNamespaceError:
            pass

        self.m_files = rtn
        return rtn

    def _get_metadata(self, filenames):

        with SocketIOTQDM(total=len(filenames), desc="Scanning files", position=0, leave=False, source=self.m_config["source"], socket=self.m_sio, event="device_status_tqdm") as main_pbar:
            file_queue = queue.Queue()
            entries_queue = queue.Queue()

            robot_name = self.m_config.get("robot_name", None)

            for item in filenames:
                file_queue.put(item)

            def worker(position:int):
                while True:
                    try:
                        dirroot, filename, fullpath = file_queue.get(block=False)
                    except queue.Empty:
                        break
                    except ValueError:
                        break

                    metadata_filename = fullpath + ".metadata"
                    if os.path.exists(metadata_filename) and (os.path.getmtime(metadata_filename) > os.path.getmtime(fullpath)):
                        device_entry = json.load(open(metadata_filename, "r"))

                    else:
                        size = os.path.getsize(fullpath)

                        metadata = getMetaData(fullpath, self.m_local_tz)
                        if metadata is None:
                            # invalid file!
                            # silently ignore invalid files! 
                            continue

                        formatted_date = getDateFromFilename(fullpath)
                        if formatted_date is None:
                            creation_date = datetime.fromtimestamp(os.path.getmtime(fullpath))
                            formatted_date = creation_date.strftime("%Y-%m-%d %H:%M:%S")
                        start_time = metadata.get("start_time", formatted_date)
                        end_time = metadata.get("end_time", formatted_date)

                        device_entry = {
                            "dirroot": dirroot,
                            "filename": filename,
                            "size": size,
                            "start_time": start_time,
                            "end_time": end_time,
                            "site": None,
                            "robot_name": robot_name,
                            "md5": None
                        }
                        device_entry.update(metadata)

                    if filename in self.m_updates:
                        device_entry.update( self.m_updates[filename])

                    entries_queue.put(device_entry)

                    try:
                        with open(metadata_filename, "w") as fid:
                            json.dump(device_entry, fid, indent=True)
                    except PermissionError as e:
                        debug_print(f"Failed to write [{metadata_filename}]. Permission Denied")
                    except Exception as e:
                        debug_print(f"Error writing [{metadata_filename}]: {e}")

                    main_pbar.update()

            threads = []
            num_threads = min(self.m_config["threads"], len(filenames))
            for i in range(num_threads):
                thread = Thread(target=worker, args=(i,))
                thread.start()
                threads.append(thread)

            for thread in threads:
                thread.join()

        entries = []
        total_size = 0
        try:
            while not entries_queue.empty():
                entry = entries_queue.get()
                entries.append(entry)
                total_size += entry["size"]
        except ValueError:
            pass
        return entries,total_size

    def _on_device_scan(self, data):
        source = data.get("source")
        if source != self.m_config["source"]:
            return

        self._scan()
        self.emitFiles()


    def _on_device_send(self, data):
        source = data.get("source")
        if source != self.m_config["source"]:
            return
        files = data.get("files")

        self._sendFiles(self.m_server, files)


    def on_device_remove(self, data):
        # debug_print(data)
        source = data.get("source")
        if source != self.m_config["source"]:
            return
        files = data.get("files")

        self._removeFiles(files)

    def _sendFiles(self, server:str, filelist:list):
        ''' Send files to server. 

            Creates up to config["threads"] workers to send files via HTTP POST to the server. 

            Args:
                server hosname:port
                filelist list[(dirroot, relpath, upload_id, offset, size)]
        '''
        self.m_signal = None

        num_threads = min(self.m_config["threads"], len(filelist))
        url = f"http://{server}/file"

        source = self.m_config["source"]
        api_key_token = self.m_config["API_KEY_TOKEN"]

        split_size_gb = self.m_config.get("split_size_gb", 1)

        total_size = 0
        file_queue = queue.Queue()
        for file_pair in filelist:
            debug_print(f"add to queue {file_pair}")
            offset = file_pair[3]
            size = file_pair[4]
            try:
                total_size += int(size) - int(offset)
            except ValueError as e:
                debug_print(file_pair)
                raise e
            file_queue.put(file_pair)

        # Outer send loop progress bar. 
        with SocketIOTQDM(total=total_size, desc="File Transfer", position=0, unit="B", unit_scale=True, leave=False, source=self.m_config["source"], socket=self.m_sio, event="device_status_tqdm") as main_pbar:

            # Worker thread. 
            # Reads the file_queue until 
            #    queue is empty 
            #    the session is disconnected
            #    the "cancel" signal is received. 
            def worker(index:int):
                with requests.Session() as session:
                    while self.isConnected():
                        try:
                            dirroot, file, upload_id, offset_, total_size = file_queue.get(block=False)
                            offset_b = int(offset_)
                            total_size = int(total_size)
                        except queue.Empty:
                            break

                        if self.m_signal == "cancel":
                            break

                        fullpath = os.path.join(dirroot, file)
                        if not os.path.exists(fullpath):
                            main_pbar.update()
                            continue

                        with open(fullpath, 'rb') as file:
                            params = {}
                            if offset_b > 0:
                                file.seek(offset_b)
                                params["offset"] = offset_b
                                total_size -= offset_b

                            split_size_b = 1024*1024*1024*split_size_gb
                            splits = total_size // split_size_b

                            params["splits"] = splits

                            headers = {
                                'Content-Type': 'application/octet-stream',
                                "X-Api-Key": api_key_token
                                }

                            # Setup the progress bar
                            with SocketIOTQDM(total=total_size, unit="B", unit_scale=True, leave=False, position=1+index, source=self.m_config["source"], socket=self.m_sio, event="device_status_tqdm") as pbar:
                                def read_and_update(offset_b, parent):

                                    read_count = 0
                                    while parent.isConnected():
                                        chunk = file.read(1024*1024)
                                        if not chunk:
                                            break
                                        yield chunk

                                        # Update the progress bars
                                        chunck_size = len(chunk)
                                        pbar.update(chunck_size)
                                        main_pbar.update(chunck_size)

                                        if self.m_signal:
                                            if self.m_signal == "cancel":
                                                break

                                        offset_b += chunck_size
                                        read_count += chunck_size

                                        if read_count >= split_size_b:
                                            break

                                for cid in range(1+splits):
                                    params["offset"] = offset_b
                                    params["cid"] = cid
                                    # Make the POST request with the streaming data
                                    response = session.post(url + f"/{source}/{upload_id}", params=params, data=read_and_update(offset_b, self), headers=headers)

                                if response.status_code != 200:
                                    print("Error uploading file:", response.text)

            # start the threads with thread id
            threads = []
            for i in range(num_threads):
                thread = Thread(target=worker, args=(i,))
                thread.start()
                threads.append(thread)

            # wait for all the threads to complete
            for thread in threads:
                thread.join()

        if self.m_signal == "cancel":
            self.emitFiles()

        self.m_signal = None


    def _removeFiles(self, files:list):

        debug_print("Enter")
        for item in files:
            dirroot, file, upload_id = item
            fullpath = os.path.join(dirroot, file)

            if os.path.exists(fullpath):
                debug_print(f"Removing {fullpath}")
                os.remove(fullpath)
                # only rename for testing. 
                # bak = fullpath + ".bak"
                # if os.path.exists(bak): 
                #     continue
                # os.rename(fullpath, bak)

            md5 = fullpath + ".md5"
            if os.path.exists(md5):
                debug_print(f"Removing {md5}")
                os.remove(md5)
                # only rename for testing. 
                # bak = md5 + ".bak"
                # if os.path.exists(bak): 
                #     continue
                # os.rename(md5, bak)

            metadata = fullpath + ".metadata"
            if os.path.exists(metadata):
                debug_print(f"Removing {metadata}")
                os.remove(metadata)
                # bak = metadata + ".bak"
                # if os.path.exists(bak): 
                #     continue
                # os.rename(md5, bak)


        self._scan()
        self.emitFiles()

    def emitFiles(self):
        '''
        Send the list of files to the server. 

        Breaks up the list into bite sized chunks. 
        '''
        if self.m_files is None:
            self._scan()
        # debug_print(files)
        if len(self.m_files) == 0:
            debug_print("No files to send")
            return None

        # clear out signals
        self.m_signal = None

        robot_name = self.m_config.get("robot_name", None)
        project = self.m_config.get("project")
        source = self.m_config["source"]

        data = {
            "robot_name": robot_name,
            "project": project,
            "source": source,
            "fs_info": self.m_fs_info,
            }


        if self.m_sio.connected:
            if project and len(project) > 1:
                N = 20
                packs = [self.m_files[i:i + N] for i in range(0, len(self.m_files), N)]
                for pack in packs:
                    self.m_sio.emit("device_files_items", {"source": source, "files": pack})
                time.sleep(0.5)
            self.m_sio.emit("device_files", data)


    def isConnected(self):
        return self.m_sio.connected

    def index(self):
        return send_from_directory("static", "index.html")

    def get_config(self):
        return jsonify(self.m_config)

    def save_config(self):
        config = request.json
        for key in config:
            if key in self.m_config:
                self.m_config[key] = config[key]
        debug_print("updated config")
        # todo: save config file.  

    def run(self):
        try:
            while True:
                server =  self._find_server()
                if server is None:
                    debug_print("Sleeping....")
                    time.sleep(self.m_config["wait_s"])
                    continue

                debug_print("loops")
                self.emitFiles()

                trigger = 0
                while self.isConnected():
                    if trigger > 10:
                        trigger = 0
                        self.m_sio.emit("keep_alive")
                    else:
                        trigger +=1
                    time.sleep(1)
                debug_print("Got disconnected!")

        except KeyboardInterrupt:
            debug_print("Terminated")
            pass

        sys.exit(0)