import logging
import pwnagotchi.plugins as plugins
import pwnagotchi.ui.components as components
import pwnagotchi.ui.view as view
from flask import Response
import random
import json
import os
import ast
import time
from concurrent.futures import ThreadPoolExecutor

class SmartAutoTune(plugins.Plugin):
    __author__ = 'Renmeii x Mr-Cass-Ette'
    __version__ = 'x88.0.1'
    __license__ = 'GPL3'
    __description__ = 'SATpwn, the superior way to capture handshakes '

    # --- Constants for configuration ---
    AP_EXPIRY_SECONDS = 3600 * 48  # 48 hours
    CLIENT_EXPIRY_SECONDS = 3600 * 24  # 24 hours
    ATTACK_SCORE_THRESHOLD = 50
    ATTACK_COOLDOWN_SECONDS = 300  # 5 minutes
    SUCCESS_BONUS_DURATION_SECONDS = 1800  # 30 minutes
    SCORE_DECAY_PENALTY_PER_HOUR = 5  # Score penalty per hour
    PMKID_FRIENDLY_APS_THRESHOLD = 3
    PMKID_FRIENDLY_BOOST_FACTOR = 1.5
    HANDSHAKE_WEIGHT = 10
    CLIENT_WEIGHT = 1
    SCORE_RECALCULATION_INTERVAL_SECONDS = 30  # 30 seconds
    EXPLORATION_PROBABILITY = 0.1  # 10% chance to explore a random channel in loose mode
    DRIVE_BY_AP_EXPIRY_SECONDS = 1800  # 30 minutes
    DRIVE_BY_CLIENT_EXPIRY_SECONDS = 900  # 15 minutes
    DRIVE_BY_ATTACK_SCORE_THRESHOLD = 20 # Lower score threshold
    DRIVE_BY_ATTACK_COOLDOWN_SECONDS = 60  # 1 minute

    def __init__(self):
        self.ready = False
        self.agent = None
        self.memory = {}
        self.modes = ['strict', 'loose', 'drive-by', 'recon']
        self.memory_path = '/etc/pwnagotchi/SATpwn_memory.json'
        self.executor = ThreadPoolExecutor(max_workers=5)
        self.mode = self.modes[0]
        self.channel_stats = {}
        self.memory_is_dirty = True

    def _save_memory(self):
        """Saves the current AP/client memory to a JSON file."""
        try:
            with open(self.memory_path, 'w') as f:
                json.dump(self.memory, f, indent=4)
            logging.info(f"[SATpwn] Memory saved to {self.memory_path}")
        except Exception as e:
            logging.error(f"[SATpwn] Error saving memory: {e}")

    def _load_memory(self):
        """Loads the AP/client memory from a JSON file."""
        if os.path.exists(self.memory_path):
            try:
                with open(self.memory_path, 'r') as f:
                    self.memory = json.load(f)
                logging.info(f"[SATpwn] Memory loaded from {self.memory_path}")
            except Exception as e:
                logging.error(f"[SATpwn] Error loading memory: {e}")

    def _cleanup_memory(self):
        """Removes old APs and clients from memory to keep it relevant."""
        self.memory_is_dirty = True
        now = time.time()

        ap_expiry = self.DRIVE_BY_AP_EXPIRY_SECONDS if self.mode == 'drive-by' else self.AP_EXPIRY_SECONDS
        client_expiry = self.DRIVE_BY_CLIENT_EXPIRY_SECONDS if self.mode == 'drive-by' else self.CLIENT_EXPIRY_SECONDS
        
        expired_aps = [ap_mac for ap_mac, data in self.memory.items()
                       if now - data.get("last_seen", 0) > ap_expiry]
        for ap_mac in expired_aps:
            if ap_mac in self.memory:
                del self.memory[ap_mac]

        for ap_mac in list(self.memory.keys()):
            if ap_mac not in self.memory: continue
            clients = self.memory[ap_mac].get("clients", {})
            expired_clients = [client_mac for client_mac, data in clients.items()
                               if now - data.get("last_seen", 0) > client_expiry]
            for client_mac in expired_clients:
                if client_mac in clients:
                    del clients[client_mac]

    def _recalculate_client_score(self, ap_mac, client_mac):
        """Calculates a client's score based on signal, success, and age."""
        client_data = self.memory[ap_mac]['clients'][client_mac]
        # Base score from signal strength
        score = (client_data.get('signal', -100) + 100)
        # Bonus for recent handshake success
        if client_data.get('last_success', 0) > time.time() - self.SUCCESS_BONUS_DURATION_SECONDS:
            score += 50
        
        # Decay score based on how long ago the client was last seen
        age_hours = (time.time() - client_data.get('last_seen', time.time())) / 3600
        decay_amount = age_hours * self.SCORE_DECAY_PENALTY_PER_HOUR
        score -= decay_amount
        
        # Ensure score doesn't go below zero
        score = max(0, score)
        
        client_data['score'] = score
        return score

    def _execute_attack(self, agent, ap_mac, client_mac):
        """Logs the intent to attack a high-value target."""
        # This method is kept simple for stability. A full implementation would need
        # to fetch the complete AP/client objects from the agent's session to
        # perform a real deauthentication attack.
        try:
            logging.info(f"[SATpwn] Executing tactical attack on {client_mac} via {ap_mac}")
        except Exception as e:
            logging.error(f"[SATpwn] Attack execution failed: {e}")

    def _get_channel_stats(self):
        """Aggregates stats per channel from memory."""
        channel_stats = {}
        for ap_mac, ap_data in self.memory.items():
            ch = ap_data.get("channel")
            if ch is None: continue
            if ch not in channel_stats:
                channel_stats[ch] = {'aps': 0, 'clients': 0, 'handshakes': 0}
            channel_stats[ch]['aps'] += 1
            channel_stats[ch]['clients'] += len(ap_data.get('clients', {}))
            channel_stats[ch]['handshakes'] += ap_data.get('handshakes', 0)
        return channel_stats

    def on_loaded(self):
        logging.info("[SATpwn] plugin loaded")
        self._load_memory()

    def on_unload(self, ui):
        self._save_memory()
        self.executor.shutdown(wait=False)
        logging.info("[SATpwn] plugin unloaded")

    def on_ready(self, agent):
        self.agent = agent
        self.ready = True
        logging.info("[SATpwn] plugin ready")

    def on_ui_setup(self, ui):
        ui.add_element('sat_mode', components.Text(color=view.BLACK, value=f'SAT Mode: {self.mode.capitalize()}',
                                                  position=(55, 120)))

    def on_ui_update(self, ui):
        ui.set('sat_mode', f'SAT Mode: {self.mode.capitalize()}')

    def on_wifi_update(self, agent, access_points):
        now = time.time()
        for ap in access_points:
            ap_mac = ap['mac'].lower()
            if ap_mac not in self.memory:
                self.memory[ap_mac] = {"ssid": ap['hostname'], "channel": ap['channel'], "clients": {}, "last_seen": now, "handshakes": 0}
            else:
                self.memory[ap_mac].update(last_seen=now, ssid=ap['hostname'], channel=ap['channel'])

            for client in ap['clients']:
                client_mac = client['mac'].lower()
                
                if client_mac not in self.memory[ap_mac]['clients']:
                    self.memory[ap_mac]['clients'][client_mac] = {
                        "last_seen": now, 
                        "signal": client['rssi'], 
                        "score": 0, 
                        "last_attempt": 0, 
                        "last_success": 0,
                        "last_recalculated": 0
                    }
                else:
                    self.memory[ap_mac]['clients'][client_mac].update(last_seen=now, signal=client['rssi'])
                
                client_data = self.memory[ap_mac]['clients'][client_mac]
                
                # Throttle score recalculation
                last_recalculated = client_data.get('last_recalculated', 0)
                if now - last_recalculated > self.SCORE_RECALCULATION_INTERVAL_SECONDS:
                    score = self._recalculate_client_score(ap_mac, client_mac)
                    client_data['last_recalculated'] = now
                else:
                    score = client_data.get('score', 0)

                last_attempt = client_data.get('last_attempt', 0)

                attack_score_threshold = self.DRIVE_BY_ATTACK_SCORE_THRESHOLD if self.mode == 'drive-by' else self.ATTACK_SCORE_THRESHOLD
                attack_cooldown = self.DRIVE_BY_ATTACK_COOLDOWN_SECONDS if self.mode == 'drive-by' else self.ATTACK_COOLDOWN_SECONDS

                if score >= attack_score_threshold and (now - last_attempt > attack_cooldown):
                    client_data['last_attempt'] = now
                    self.executor.submit(self._execute_attack, agent, ap_mac, client_mac)
        
        self.memory_is_dirty = True

    def on_handshake(self, agent, filename, ap, client):
        ap_mac = ap['mac'].lower()
        if ap_mac in self.memory:
            self.memory[ap_mac]['handshakes'] = self.memory[ap_mac].get('handshakes', 0) + 1
        
        client_mac = client['mac'].lower()
        if ap_mac in self.memory and client_mac in self.memory[ap_mac]['clients']:
            self.memory[ap_mac]['clients'][client_mac]['last_success'] = time.time()
            self._recalculate_client_score(ap_mac, client_mac)
        
        self.memory_is_dirty = True

    def _number_reader(numbers):
        """Generator that yields numbers one by one and returns 'end' when done."""
        for num in numbers:
            yield num
        yield "end"



#code for all of the modes (START)
    def _epoch_strict(self, agent, epoch, epoch_data):
        if self.memory_is_dirty or not self.channel_stats:
            self.channel_stats = self._get_channel_stats()
            self.memory_is_dirty = False

        # Weighted selection logic
        channels = list(self.channel_stats.keys())
        if not channels:
            next_channel = random.choice(supported_channels)
            logging.info(f"[SATpwn] No channel data, hopping to random channel {next_channel}")
            agent.set_channel(next_channel)
            return

        weights = []
        for ch in channels:
            stats = self.channel_stats.get(ch, {'clients': 0, 'handshakes': 0, 'aps': 0})
            weight = (stats['clients'] * self.CLIENT_WEIGHT) + (stats['handshakes'] * self.HANDSHAKE_WEIGHT)
            if stats['aps'] > self.PMKID_FRIENDLY_APS_THRESHOLD and stats['aps'] > stats['clients']:
                weight *= self.PMKID_FRIENDLY_BOOST_FACTOR
            weights.append(weight)

        # Filter down to only channels that are supported by the hardware
        supported_channels_with_weights = []
        supported_weights = []
        for i, ch in enumerate(channels):
            if ch in supported_channels:
                supported_channels_with_weights.append(ch)
                supported_weights.append(weights[i])

        if not supported_channels_with_weights:
            next_channel = random.choice(supported_channels)
            logging.info(f"[SATpwn] No tracked channels are supported, hopping to random supported channel {next_channel}")
        else:
            total_weight = sum(supported_weights)
            if total_weight == 0:
                next_channel = random.choice(supported_channels_with_weights)
                logging.info(f"[SATpwn] All tracked channel weights are zero, hopping to random tracked/supported channel {next_channel}")
            else:
                next_channel = random.choices(supported_channels_with_weights, weights=supported_weights, k=1)[0]
                logging.info(f"[SATpwn] Hopping to weighted-random channel {next_channel} (Mode: {self.mode})")
        
        agent.set_channel(next_channel)

    def _epoch_loose(self, agent, epoch, epoch_data):
        if self.memory_is_dirty or not self.channel_stats:
            self.channel_stats = self._get_channel_stats()
            self.memory_is_dirty = False

        if random.random() < self.EXPLORATION_PROBABILITY:
            next_channel = random.choice(supported_channels)
            logging.info(f"[SATpwn] Exploring random channel {next_channel} (Mode: loose)")
            agent.set_channel(next_channel)
            return

        # Weighted selection logic
        channels = list(self.channel_stats.keys())
        if not channels:
            next_channel = random.choice(supported_channels)
            logging.info(f"[SATpwn] No channel data, hopping to random channel {next_channel}")
            agent.set_channel(next_channel)
            return

        weights = []
        for ch in channels:
            stats = self.channel_stats.get(ch, {'clients': 0, 'handshakes': 0, 'aps': 0})
            weight = (stats['clients'] * self.CLIENT_WEIGHT) + (stats['handshakes'] * self.HANDSHAKE_WEIGHT)
            if stats['aps'] > self.PMKID_FRIENDLY_APS_THRESHOLD and stats['aps'] > stats['clients']:
                weight *= self.PMKID_FRIENDLY_BOOST_FACTOR
            weights.append(weight)

        exploration_bonus = 1.0
        weights = [w + exploration_bonus for w in weights]
        logging.info("[SATpwn] Applied exploration bonus for loose mode.")

        # Filter down to only channels that are supported by the hardware
        supported_channels_with_weights = []
        supported_weights = []
        for i, ch in enumerate(channels):
            if ch in supported_channels:
                supported_channels_with_weights.append(ch)
                supported_weights.append(weights[i])

        if not supported_channels_with_weights:
            next_channel = random.choice(supported_channels)
            logging.info(f"[SATpwn] No tracked channels are supported, hopping to random supported channel {next_channel}")
        else:
            total_weight = sum(supported_weights)
            if total_weight == 0:
                next_channel = random.choice(supported_channels_with_weights)
                logging.info(f"[SATpwn] All tracked channel weights are zero, hopping to random tracked/supported channel {next_channel}")
            else:
                next_channel = random.choices(supported_channels_with_weights, weights=supported_weights, k=1)[0]
                logging.info(f"[SATpwn] Hopping to weighted-random channel {next_channel} (Mode: {self.mode})")
        
        agent.set_channel(next_channel)

    def _epoch_driveby(self, agent, epoch, epoch_data):
        self._epoch_strict() #called strict, as the only diffrence is the varibles. (TODO: add varibles inside of each function)

    def _epoch_recon(self, agent, epoch, epoch_data):
        supported_channels = agent.supported_channels()
        reader = self._number_reader(supported_channels)
        if reader != "end"
            logging.info(f"[SATpwn] RECON: inspecting channel {reader} and gathering info...")
            agent.set_channel(reader)
        else:
            #idk what logic to do in order to determine the highest-grossing channel, you know this code better than me.

#code for all of the modes (END)
    def on_epoch(self, agent, epoch, epoch_data):
        self._cleanup_memory()
        if not self.ready:
            return

        supported_channels = agent.supported_channels()
        logging.info(f"{supported_channels}")

        if not supported_channels:
            logging.info(f"{supported_channels}")
            logging.warning("[SATpwn] No supported channels found.")
            return

        if self.mode == 'loose':
            logging.info("Epoch done; loading loose mode")
            self._epoch_loose(self, agent, epoch, epoch_data)
            
        elif self.mode == 'drive-by':
            logging.info("Epoch done; loading drive-by mode")
            self._epoch_driveby(self, agent, epoch, epoch_data)

        elif self.mode == 'recon':
            logging.info("Epoch done; loading recon mode")
            self._epoch_recon(self, agent, epoch, epoch_data)

        else:
            logging.info("Epoch done; loading strict mode")
            self._epoch_strict()


    def on_webhook(self, path, request):
        if not self.ready:
            return Response("Plugin not ready yet.", mimetype='text/html')

        # Handle mode toggling
        if path == 'toggle_mode':
            current_index = self.modes.index(self.mode)
            logging.debug(f"current index = {current_index}")
            next_index = (current_index + 1) % len(self.modes)
            logging.debug(f"next index = {next_index}")
            self.mode = self.modes[next_index]
            logging.info(f"[SATpwn] Mode changed to {self.mode}")
            return Response('<html><head><meta http-equiv="refresh" content="0; url=/plugins/SATpwn/" /></head></html>', mimetype='text/html')

        # Main dashboard page
        if path == '/' or not path:
            if self.memory_is_dirty or not self.channel_stats:
                self.channel_stats = self._get_channel_stats()
                self.memory_is_dirty = False
            
            total_aps = len(self.memory)
            total_clients = sum(len(ap.get('clients', {})) for ap in self.memory.values())

            channel_html = "<table><tr><th>Ch</th><th>APs</th><th>Clients</th><th>Handshakes</th></tr>"
            for ch, stats in sorted(self.channel_stats.items()):
                channel_html += f"<tr><td>{ch}</td><td>{stats['aps']}</td><td>{stats['clients']}</td><td>{stats['handshakes']}</td></tr>"
            channel_html += "</table>"

            memory_html = "<table><tr><th>AP (SSID/MAC)</th><th>Ch</th><th>Last Seen</th><th>Clients (MAC / Score / Last Seen)</th></tr>"
            sorted_aps = sorted(self.memory.items(), key=lambda item: item[1].get('last_seen', 0), reverse=True)
            for ap_mac, ap_data in sorted_aps:
                last_seen_ap = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ap_data.get('last_seen', 0)))
                client_html = "<ul style='margin:0;padding-left:15px;'>"
                sorted_clients = sorted(ap_data.get('clients', {}).items(), key=lambda item: item[1].get('score', 0), reverse=True)
                for client_mac, client_data in sorted_clients:
                    last_seen_client = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(client_data.get('last_seen', 0)))
                    score = client_data.get('score', 0)
                    client_html += f"<li><small>{client_mac} | Score: {score:.2f} | Seen: {last_seen_client}</small></li>"
                client_html += "</ul>"
                memory_html += f"<tr><td>{ap_data.get('ssid', 'N/A')}<br><small>{ap_mac}</small></td><td>{ap_data.get('channel', 'N/A')}</td><td>{last_seen_ap}</td><td>{client_html}</td></tr>"
            memory_html += "</table>"
            
            next_mode_index = (self.modes.index(self.mode) + 1) % len(self.modes)
            next_mode_name = self.modes[next_mode_index].capitalize()
            mode_toggle_button = f"<a href='/plugins/SATpwn/toggle_mode' style='display:inline-block;padding:10px;background-color:#569cd6;color:#fff;text-decoration:none;border-radius:5px;'>Switch to {next_mode_name} Mode</a>"

            html = f"""
            <html>
            <head>
                <title>Smart Auto-Tune Dashboard</title>
                <style>
                    body {{ font-family: monospace; background-color: #1e1e1e; color: #d4d4d4; margin: 0; padding: 20px; }}
                    .container {{ display: grid; grid-template-columns: 1fr; gap: 20px; }}
                    .grid-2-col {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
                    .card {{ background-color: #252526; border: 1px solid #333; border-radius: 5px; padding: 15px; }}
                    h1, h2 {{ color: #569cd6; border-bottom: 1px solid #333; padding-bottom: 5px;}}
                    table {{ width: 100%; border-collapse: collapse; }}
                    th, td {{ border: 1px solid #444; padding: 8px; text-align: left; vertical-align: top;}}
                    th {{ background-color: #333; }}
                    ul {{ list-style-type: none; margin: 0; padding-left: 15px;}}
                </style>
            </head>
            <body>
                <h1>SATpwn dashboard</h1>
                <div class="container">
                    <div class="grid-2-col">
                        <div class="card">
                            <h2>Live Stats</h2>
                            <p>Total APs Tracked: {total_aps}</p>
                            <p>Total Clients Tracked: {total_clients}</p>
                        </div>
                        <div class="card">
                            <h2>Controls</h2>
                            <p><b>Current Mode:</b> {self.mode.upper()}</p>
                            {mode_toggle_button}
                        </div>
                    </div>
                    <div class="card">
                        <h2>Channel Weights</h2>
                        {channel_html}
                    </div>
                    <div class="card">
                        <h2>AP & Client Memory</h2>
                        {memory_html}
                    </div>
                </div>
            </body>
            </html>
            """
            return Response(html, mimetype='text/html')
        
        return Response("Not Found", status=404, mimetype='text/html')
