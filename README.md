# SATpwn Plugin for Pwnagotchi

The SATpwn plugin is an advanced tool for Pwnagotchi that replaces the default decision-making process with an intelligent, adaptive learning system. It uses a client scoring system to focus on high-value targets and a history-based, weighted algorithm to select the most productive Wi-Fi channels.

This plugin was built to be a custom, lightweight alternative to other advanced plugins, focusing on efficient channel hopping and tactical client selection. It now features multiple operating modes to adapt to different environments, from stationary analysis to mobile handshake capture, and includes a sophisticated "auto" mode that determines activity to make intelligent decisions.

## Features

-   **Tactical Client Scoring:** Dynamically scores clients based on signal strength, recent activity, and handshake success.
-   **Intelligent Channel Hopping:** Moves away from simple sequential hopping. It analyzes the historical success rate of each channel to maximize efficiency.
-   **Multiple Operating Modes:**
    -   **Strict:** The default mode. Focuses purely on the channels with the highest calculated weight, ideal for stationary use in a familiar area.
    -   **Loose:** A balanced mode that adds an exploration bonus to all channels, preventing over-focusing and allowing for discovery of new networks. It also has a small chance to jump to a completely random channel.
    -   **Drive-by:** A high-aggression mode designed for mobile use (e.g., walking, cycling, driving). It uses shorter memory expiry times and more frequent, lower-threshold attacks to maximize handshake capture opportunities in a rapidly changing environment.
    -   **Recon:** A passive mode for mapping out the Wi-Fi environment. It systematically cycles through all supported channels to gather intelligence without performing any attacks.
    -   **Auto:** An advanced mode. It automatically switches between `recon`, `drive-by`, and `strict`/`loose` modes based on what the device sees, or it's activity.
-   **On-Screen Display:** Shows the current operating mode directly on the Pwnagotchi's e-ink screen for at-a-glance status awareness.
-   **Performance Optimized:** Caching layers and calculation throttling have been implemented to reduce CPU load and ensure the plugin runs smoothly on all Raspberry Pi models.
-   **Persistent Memory:** Remembers all seen access points and clients by saving its memory to a JSON file.
-   **Tactical Dashboard:** A comprehensive web UI to monitor the plugin's status, live stats, channel weights, and a detailed view of the AP/client memory. The dashboard also displays live GPS data.
-   **Exclusive Mode:** "main.disable_defaults = true" to ensure SATpwn is tthe only scanning and attack logic!
## How It Works

### 1. AP & Client Memory
The plugin maintains a memory of every access point and client it encounters in a JSON file located at `/etc/pwnagotchi/SATpwn_memory.json`. This file stores SSIDs, channels, signal strengths, handshake counts, timestamps, and the home anchor point.

### 2. Client Scoring
Each client is assigned a score to determine its value as a target. The score is calculated based on signal strength, recent handshake success, and a linear decay based on age.

### 3. Channel Selection & Modes
At the beginning of each epoch, the plugin calculates a "weight" for every channel based on the number of clients and captured handshakes. The behavior then changes based on the selected mode:
-   **Strict Mode:** The plugin will perform a weighted random selection, heavily favoring channels that have been historically productive.
-   **Loose Mode:** The plugin adds an "exploration bonus" to the weight of all channels, making it more likely to try less common channels. It also has a 10% chance to jump to a completely random supported channel to discover new networks.
-   **Drive-by Mode:** This mode uses much shorter expiry times for APs (30 min) and clients (15 min). It also uses a lower score threshold and a shorter cooldown for initiating attacks, making it highly aggressive.
-   **Recon Mode:** Systematically cycles through all supported channels to gather data without performing any attacks.
-   **Auto Mode:** This is where the activity based dtection comes into play. The plugin uses the following logic to decide which sub-mode to use:
    -   If the device is moving, it will go into driveby mode.
    -   If the device is stationary, it will go into recon mode.
    -   If the device sees a lot of data, it will go into strict mode
    -   If the device sees a not a whole lot of data, it will go into loose mode.


## Installation

1.  Place the `SATpwn.py` file into your Pwnagotchi's custom plugin directory (this is usually `/usr/local/share/pwnagotchi/custom-plugins/`).
2.  Open your `config.toml` file and add the following line under the `main.plugins` section:
    ```toml
    main.plugins.SATpwn = true
    main.disable_defaults = true -- set this to true so SATpwn is the only scanning and running attack logic.
    ```
3.  To use the home detection features, you must add your home SSIDs to the `main.home_whitelist` list in your `config.toml`:
    ```toml
    main.home_whitelist = [
      "MyHomeSSID",
      "AnotherHomeNetwork"
    ]
    ```
4.  Restart your Pwnagotchi service to apply the changes:
    ```bash
    sudo systemctl restart pwnagotchi
    ```

**Important:** This plugin controls channel hopping and attacks. It should be used **standalone** and not at the same time as other plugins that perform similar functions.

## The UI

### On-Screen Display
The plugin will display its current mode in the top-left corner of the Pwnagotchi's e-ink screen (e.g., "SAT Mode: Auto (recon)").

### Tactical Dashboard
You can access the dashboard by navigating to `http://<your-pwnagotchi-ip>:8080/plugins/SATpwn/`. The dashboard features:
-   **Live Stats Card:** A quick overview of the total number of unique APs and clients being tracked.
-   **Controls Card:** Shows the current mode and provides a button to cycle to the next mode. When in "auto" mode, this card displays detailed 
-   **Channel Weights Card:** A table showing the statistics for each channel.
-   **AP & Client Memory Card:** A detailed, sortable table of all remembered APs and their clients.

## License

This plugin is released under the **GPLv3 license**.
