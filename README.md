<!--
  PROJECT HEADER
-->
<p align="center">
  <a href="https://github.com/InvenioX3/qbit_airdrop">
    <!-- PROJECT ICON PLACEHOLDER -->
    <img src="images/magnet.png" alt="Qbit Airdrop Integration" width="120" />
  </a>
</p>

<h1 align="center">Qbit Airdrop – Home Assistant Integration for qBittorrent torrent management.</h1>

<p align="center">
  <em>Paste, parse, and airdrop magnet links to qBittorrent via Home Assistant custom lovelace card.</em>
</p>

<p align="center">
  <!-- BADGES: COLORS & LINKS -->
  <a href="https://hacs.xyz/">
    <img
      alt="HACS Custom Integration"
      src="https://img.shields.io/badge/HACS-Custom%20Integration-41BDF5?style=for-the-badge&logo=homeassistantcommunitystore&logoColor=white"
    />
  </a>
  <a href="https://github.com/InvenioX3/qbit_airdrop/releases">
    <img
      alt="Version"
      src="https://img.shields.io/github/v/release/InvenioX3/qbit_airdrop?style=for-the-badge&color=0A84FF"
    />
  </a>
  <a href="https://github.com/InvenioX3">
    <img
      alt="Author"
      src="https://img.shields.io/badge/author-JosephBrandenburg-9A4DFF?style=for-the-badge"
    />
  </a>
  <a href="https://github.com/InvenioX3/qbit_airdrop/releases">
    <img
      alt="Downloads"
      src="https://img.shields.io/github/downloads/InvenioX3/qbit_airdrop/total?style=for-the-badge&color=34C759"
    />
  </a>
  <a href="https://github.com/InvenioX3/qbit_airdrop/issues">
    <img
      alt="Open Issues"
      src="https://img.shields.io/github/issues/InvenioX3/qbit_airdrop?style=for-the-badge&color=FF9500"
    />
  </a>
  <a href="https://github.com/InvenioX3/qbit_airdrop/stargazers">
    <img
      alt="Stars"
      src="https://img.shields.io/github/stars/InvenioX3/qbit_airdrop?style=for-the-badge&color=FFD60A"
    />
  </a>
</p>

<p align="center">
  <!-- QUICK META LINKS -->
  <a href="#overview"><strong>Overview</strong></a> ·
  <a href="#features"><strong>Features</strong></a> ·
  <a href="#installation"><strong>Installation</strong></a> ·
  <a href="#configuration"><strong>Configuration</strong></a> ·
  <a href="#services--endpoints"><strong>Services & Endpoints</strong></a> ·
  <a href="#lovelace-card"><strong>Lovelace Card</strong></a> ·
  <a href="#related-repositories"><strong>Related Repos</strong></a>
</p>

---

## Overview

**Qbit Airdrop** is a Home Assistant custom integration that connects your Home Assistant instance to a local **qBittorrent** client via its WebUI API.

The integration focuses on one job:

> Make it trivial to “airdrop” magnet links to qBittorrent from the native HA app on mobile devices, while keeping the backend logic and category/save-path management automated.

The integration exposes:

- A simple **service** for adding magnet links (`qbit_airdrop.add_magnet`).
- A **reload** helper service (`qbit_airdrop.reload_entry`).
- Lightweight **HTTP endpoints** for:
  - Listing active torrents.
  - Deleting torrents (with or without files).

It is designed to be used together with the **Qbit Airdrop Card** Lovelace card, which provides the UI for pasting magnet links and managing active torrents directly from a dashboard.

---

## Features

- **Direct qBittorrent WebUI integration**
  - Talks to qBittorrent’s HTTP API using the configured host and port.
  - Builds a normalized base URL even if you provide a bare host or a full URL.

- **Magnet submission service**
  - `qbit_airdrop.add_magnet` accepts a magnet link and an optional category.
  - If a category and `base_path` are provided, the integration:
    - Computes a per-category save path.
    - Ensures the category exists in qBittorrent with that save path.
    - Submits the magnet with both category and save path.

- **Automatic per-category save locations**
  - When configured with a `base_path`, the integration constructs:
    ```text
    <base_path>/<category>/
    ```
  - It calls qBittorrent’s `createCategory` API with this path.
  - qBittorrent automatically creates and uses that folder as the download location.

- **Active torrent listing**
  - Exposes an HTTP endpoint under Home Assistant that returns:
    - Name (cleaned display name).
    - Progress (converted to percentage).
    - State (e.g., Downloading, Seeding, Paused).
    - Size, download speed, hash.
    - Availability (used by the card for coloring).

- **Torrent deletion helpers**
  - HTTP endpoint for removing torrents by hash:
    - Optionally deletes files on disk.
    - Used by the Lovelace card for one-click actions.

- **Designed for dashboards**
  - Lightweight JSON endpoints and service calls that are easy to consume from Lovelace.
  - Works particularly well when paired with the **Qbit Airdrop Submit Card**.

---

## Requirements

- Home Assistant (core) with the ability to install custom integrations.
- HACS installed and configured.
- A reachable **qBittorrent** instance with WebUI enabled:
  - Host and port accessible from your Home Assistant instance.
  - WebUI credentials and access configured so that Home Assistant can call the API.
- (Optional but recommended) A base path on storage (e.g., a NAS mount) that will hold your categorized downloads.

---

## Installation

### 1. Install via HACS (recommended)

1. Open **HACS → Integrations**.
2. Add this repository as a **Custom Repository** if it is not yet in the default HACS store:
   - Repository: `https://github.com/InvenioX3/qbit_airdrop`
   - Category: `Integration`
3. Search for **Qbit Airdrop** in HACS and install it.
4. Restart Home Assistant when prompted.

### 2. Manual installation (advanced)

1. Copy the `custom_components/qbit_airdrop` folder from this repository into:
   ```text
   <config>/custom_components/qbit_airdrop
