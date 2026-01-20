#!/usr/bin/env python3
"""
Music Tagger - Web app to tag music albums
Features: Edit metadata, album art, history, undo, search, web metadata lookup, audio fingerprinting
"""

import os
import json
import shutil
import requests
from datetime import datetime
from pathlib import Path
from collections import Counter
from flask import Flask, render_template_string, request, jsonify, redirect, url_for, Response, send_file
from mutagen.flac import FLAC, Picture
import base64
import urllib.parse
from PIL import Image
from io import BytesIO
import musicbrainzngs
import acoustid

app = Flask(__name__)

# Configuration
MUSIC_DIR = "/home/arm/music"
UNKNOWN_DIR = os.path.join(MUSIC_DIR, "Unknown Artist")
HISTORY_FILE = "/home/arm/logs/tagger_history.json"
BACKUP_DIR = "/home/arm/logs/tagger_backups"
# API key loaded from environment variable (set in .env file or docker-compose)
ACOUSTID_API_KEY = os.environ.get("ACOUSTID_API_KEY", "")

# Setup MusicBrainz
musicbrainzngs.set_useragent("MusicTagger", "1.0", "tagger@localhost")

os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)

# =============================================================================
# HISTORY FUNCTIONS
# =============================================================================

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r') as f:
                return json.load(f)
        except:
            return []
    return []

def save_history(history):
    with open(HISTORY_FILE, 'w') as f:
        json.dump(history, f, indent=2)

def add_history_entry(entry):
    history = load_history()
    entry['id'] = len(history) + 1
    entry['timestamp'] = datetime.now().isoformat()
    entry['undone'] = False
    history.append(entry)
    if len(history) > 100:
        history = history[-100:]
    save_history(history)
    return entry['id']

def get_last_undoable():
    history = load_history()
    for entry in reversed(history):
        if not entry.get('undone') and entry.get('backup_path'):
            return entry
    return None

# =============================================================================
# MUSICBRAINZ LOOKUP
# =============================================================================

def fetch_musicbrainz_metadata(artist_query, album_query):
    """Search MusicBrainz for album metadata"""
    try:
        # Search for the release
        result = musicbrainzngs.search_releases(
            artist=artist_query,
            release=album_query,
            limit=5
        )
        
        if not result.get('release-list'):
            return None
        
        # Get the best match
        release = result['release-list'][0]
        release_id = release['id']
        
        # Get full release details including tracks
        full_release = musicbrainzngs.get_release_by_id(
            release_id,
            includes=['recordings', 'artists', 'release-groups']
        )['release']
        
        # Extract metadata
        metadata = {
            'artist': full_release.get('artist-credit-phrase', artist_query),
            'album': full_release.get('title', album_query),
            'year': '',
            'genre': '',
            'tracks': [],
            'art_url': None
        }
        
        # Get year from date
        date = full_release.get('date', '')
        if date:
            metadata['year'] = date[:4]
        
        # Get tracks from medium-list
        if 'medium-list' in full_release:
            for medium in full_release['medium-list']:
                if 'track-list' in medium:
                    for track in medium['track-list']:
                        recording = track.get('recording', {})
                        title = recording.get('title', track.get('title', ''))
                        if title:
                            metadata['tracks'].append(title)
        
        # Try to get cover art URL from Cover Art Archive
        art_url = None
        try:
            release_art_url = f"https://coverartarchive.org/release/{release_id}/front-250"
            if requests.head(release_art_url, timeout=3, allow_redirects=True).status_code == 200:
                art_url = release_art_url
        except:
            pass
        
        # Fallback to release-group art if no release art
        if not art_url:
            try:
                rg = full_release.get('release-group', {})
                rg_id = rg.get('id')
                if rg_id:
                    rg_art_url = f"https://coverartarchive.org/release-group/{rg_id}/front-250"
                    if requests.head(rg_art_url, timeout=3, allow_redirects=True).status_code == 200:
                        art_url = rg_art_url
            except:
                pass
        
        metadata['art_url'] = art_url
        return metadata
        
    except Exception as e:
        print(f"MusicBrainz lookup error: {e}")
        return None

# =============================================================================

def search_musicbrainz_releases(artist_query, album_query, local_track_count=0):
    """Search MusicBrainz and return top 5 candidates for user to choose"""
    try:
        result = musicbrainzngs.search_releases(artist=artist_query, release=album_query, limit=10)
        if not result.get('release-list'):
            return []
        candidates = []
        seen = set()
        for release in result['release-list']:
            rid = release['id']
            artist = release.get('artist-credit-phrase', 'Unknown')
            title = release.get('title', 'Unknown')
            date = release.get('date', '')
            year = date[:4] if date else ''
            track_count = sum(int(m.get('track-count', 0)) for m in release.get('medium-list', []))
            key = f"{artist.lower()}|{title.lower()}|{track_count}"
            if key in seen:
                continue
            seen.add(key)
            track_match = (track_count == local_track_count) if local_track_count > 0 else False
            candidates.append({'id': rid, 'artist': artist, 'album': title, 'year': year, 'track_count': track_count, 'track_match': track_match, 'score': release.get('ext:score', 0)})
            if len(candidates) >= 5:
                break
        candidates.sort(key=lambda x: (not x['track_match'], -int(x.get('score', 0))))
        return candidates
    except Exception as e:
        print(f"MusicBrainz search error: {e}")
        return []

def fetch_release_details(release_id):
    """Get full details for a specific MusicBrainz release"""
    try:
        full = musicbrainzngs.get_release_by_id(release_id, includes=['recordings', 'artists', 'release-groups', 'artist-credits'])['release']
        album_artist = full.get('artist-credit-phrase', '')
        meta = {'artist': album_artist, 'album': full.get('title', ''), 'year': '', 'genre': '', 'tracks': [], 'art_url': None}
        date = full.get('date', '')
        if date:
            meta['year'] = date[:4]
        
        # Check if this is a compilation (Various Artists, etc.)
        is_compilation = album_artist.lower() in ['various artists', 'various', 'va', 'soundtrack', 'ost']
        
        for medium in full.get('medium-list', []):
            for track in medium.get('track-list', []):
                recording = track.get('recording', {})
                title = recording.get('title', track.get('title', ''))
                
                # Get per-track artist if available
                track_artist = ''
                artist_credit = recording.get('artist-credit', []) or track.get('artist-credit', [])
                if artist_credit:
                    track_artist = ''.join([
                        ac.get('artist', {}).get('name', '') + ac.get('joinphrase', '')
                        for ac in artist_credit
                    ]).strip()
                
                # For compilations or when track artist differs, format as "Artist - Title"
                if title:
                    if track_artist and (is_compilation or track_artist != album_artist):
                        meta['tracks'].append(f"{track_artist} - {title}")
                    else:
                        meta['tracks'].append(title)
        
        # Try to get album art - first from specific release, then from release-group
        art_url = None
        try:
            # Try specific release first
            release_art_url = f"https://coverartarchive.org/release/{release_id}/front-250"
            resp = requests.head(release_art_url, timeout=3, allow_redirects=True)
            if resp.status_code == 200:
                art_url = release_art_url
                print(f"[ART] Found release art: {art_url}")
        except Exception as e:
            print(f"[ART] Release art check failed: {e}")
        
        # If no release art, try release-group art
        if not art_url:
            try:
                release_group = full.get('release-group', {})
                rg_id = release_group.get('id')
                if rg_id:
                    rg_art_url = f"https://coverartarchive.org/release-group/{rg_id}/front-250"
                    resp = requests.head(rg_art_url, timeout=3, allow_redirects=True)
                    if resp.status_code == 200:
                        art_url = rg_art_url
                        print(f"[ART] Found release-group art: {art_url}")
            except Exception as e:
                print(f"[ART] Release-group art check failed: {e}")
        
        meta['art_url'] = art_url
        return meta
    except Exception as e:
        print(f"MusicBrainz fetch error: {e}")
        return None


# ALBUM COUNT HELPERS
# =============================================================================

def get_album_counts():
    """Get counts of unknown and total albums for display in tabs"""
    unknown_count = 0
    total_count = 0
    
    # Count unknown albums
    if os.path.exists(UNKNOWN_DIR):
        for folder in os.listdir(UNKNOWN_DIR):
            folder_path = os.path.join(UNKNOWN_DIR, folder)
            if os.path.isdir(folder_path) and list(Path(folder_path).glob("*.flac")):
                unknown_count += 1
    
    # Count all albums (including flat structure)
    if os.path.exists(MUSIC_DIR):
        for item in os.listdir(MUSIC_DIR):
            item_path = os.path.join(MUSIC_DIR, item)
            if os.path.isdir(item_path) and item != "Unknown Artist":
                flac_files = list(Path(item_path).glob("*.flac"))
                if flac_files:
                    total_count += 1  # Flat album
                else:
                    for album_name in os.listdir(item_path):
                        album_path = os.path.join(item_path, album_name)
                        if os.path.isdir(album_path) and list(Path(album_path).glob("*.flac")):
                            total_count += 1
    
    return {'unknown': unknown_count, 'total': total_count}


# SEARCH FUNCTION
# =============================================================================

def search_library(query):
    results = []
    query_lower = query.lower().strip()
    
    if not query_lower or len(query_lower) < 2:
        return results
    
    if os.path.exists(MUSIC_DIR):
        for artist in os.listdir(MUSIC_DIR):
            artist_path = os.path.join(MUSIC_DIR, artist)
            if not os.path.isdir(artist_path):
                continue
                
            for album_name in os.listdir(artist_path):
                album_path = os.path.join(artist_path, album_name)
                if not os.path.isdir(album_path):
                    continue
                
                flac_files = list(Path(album_path).glob("*.flac"))
                if not flac_files:
                    continue
                
                artist_match = query_lower in artist.lower()
                album_match = query_lower in album_name.lower()
                
                matching_tracks = []
                track_artist_match = False
                for f in flac_files:
                    try:
                        audio = FLAC(str(f))
                        title = audio.get('title', [f.stem])[0]
                        track_artist = audio.get('artist', [''])[0]
                        
                        # Check if query matches track title or per-track artist
                        if query_lower in title.lower():
                            matching_tracks.append(title)
                        elif track_artist and query_lower in track_artist.lower():
                            # Track artist match - show as "Artist - Title"
                            matching_tracks.append(f"{track_artist} - {title}")
                            track_artist_match = True
                    except:
                        pass
                
                if artist_match or album_match or matching_tracks or track_artist_match:
                    is_unknown = artist == "Unknown Artist"
                    results.append({
                        'artist': artist,
                        'album_name': album_name,
                        'path': album_path,
                        'track_count': len(flac_files),
                        'matching_tracks': matching_tracks[:3],
                        'match_type': 'artist' if artist_match else ('album' if album_match else 'track'),
                        'is_unknown': is_unknown
                    })
    
    results.sort(key=lambda x: (
        0 if query_lower == x['artist'].lower() or query_lower == x['album_name'].lower() else 1,
        x['artist'].lower()
    ))
    
    return results[:50]

# =============================================================================
# HTML TEMPLATE
# =============================================================================
HTML_TEMPLATE = r'''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Pi Music Manager</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            min-height: 100vh;
            color: #e0e0e0;
            padding: 20px;
        }
        
        .container { max-width: 900px; margin: 0 auto; }
        
        h1 { text-align: center; color: #4ecca3; margin-bottom: 16px; font-size: 2rem; }
        
        .search-container { margin-bottom: 20px; }
        
        .search-box {
            display: flex;
            gap: 8px;
            background: rgba(0,0,0,0.3);
            padding: 12px;
            border-radius: 10px;
            border: 1px solid rgba(255,255,255,0.1);
        }
        
        .search-input {
            flex: 1;
            padding: 12px 16px;
            font-size: 1rem;
            border: 2px solid rgba(255,255,255,0.1);
            border-radius: 8px;
            background: rgba(0,0,0,0.3);
            color: #fff;
        }
        
        .search-input:focus { outline: none; border-color: #4ecca3; }
        .search-input::placeholder { color: #666; }
        
        .search-btn {
            padding: 12px 24px;
            font-size: 1rem;
            font-weight: 600;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            background: #4ecca3;
            color: #1a1a2e;
        }
        
        .search-btn:hover { background: #3db892; }
        
        .search-clear {
            padding: 12px 16px;
            font-size: 1rem;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            background: rgba(255,255,255,0.1);
            color: #888;
        }
        
        .search-clear:hover { background: rgba(255,255,255,0.2); color: #fff; }
        
        .refresh-btn { padding: 12px 14px; border: none; border-radius: 8px; cursor: pointer; background: rgba(78,204,163,0.2); color: #4ecca3; display: flex; align-items: center; }
        .refresh-btn:hover { background: rgba(78,204,163,0.4); }
        .refresh-btn svg { width: 20px; height: 20px; }
        
        .tabs {
            display: flex;
            gap: 4px;
            margin-bottom: 20px;
            background: rgba(0,0,0,0.2);
            padding: 6px;
            border-radius: 10px;
        }
        
        .tab {
            flex: 1;
            padding: 12px 16px;
            text-align: center;
            background: transparent;
            border: none;
            color: #888;
            font-size: 1rem;
            cursor: pointer;
            border-radius: 8px;
            text-decoration: none;
        }
        
        .tab:hover { color: #e0e0e0; background: rgba(255,255,255,0.05); }
        .tab.active { background: #4ecca3; color: #1a1a2e; font-weight: 600; }
        
        .card {
            background: rgba(255,255,255,0.05);
            border-radius: 12px;
            padding: 24px;
            margin-bottom: 20px;
            border: 1px solid rgba(255,255,255,0.1);
        }
        
        .card-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 16px;
            padding-bottom: 12px;
            border-bottom: 1px solid rgba(255,255,255,0.1);
        }
        
        .card-title { color: #4ecca3; font-weight: 600; font-size: 1rem; }
        
        .album-list { list-style: none; }
        
        .album-item {
            background: rgba(78, 204, 163, 0.1);
            border: 1px solid rgba(78, 204, 163, 0.3);
            border-radius: 8px;
            padding: 16px 20px;
            margin-bottom: 12px;
            cursor: pointer;
            transition: all 0.2s;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .album-item:hover { background: rgba(78, 204, 163, 0.2); transform: translateX(5px); }
        .album-item.warning { background: rgba(255, 193, 7, 0.1); border-color: rgba(255, 193, 7, 0.3); }
        .album-item.warning:hover { background: rgba(255, 193, 7, 0.2); }
        
        .album-name { font-size: 1.1rem; font-weight: 500; }
        .album-artist { color: #4ecca3; font-size: 0.9rem; margin-top: 4px; }
        .track-count { color: #888; font-size: 0.9rem; text-align: right; flex-shrink: 0; }
        
        .album-item-with-art { display: flex; align-items: center; gap: 14px; }
        .album-thumb {
            width: 50px;
            height: 50px;
            background: rgba(0,0,0,0.3);
            border-radius: 6px;
            flex-shrink: 0;
            display: flex;
            align-items: center;
            justify-content: center;
            overflow: hidden;
            color: #444;
        }
        .album-thumb svg { width: 24px; height: 24px; }
        .album-thumb img { width: 100%; height: 100%; object-fit: cover; }
        .album-info { flex: 1; min-width: 0; }
        .album-info .album-name, .album-info .album-artist { 
            white-space: nowrap; 
            overflow: hidden; 
            text-overflow: ellipsis; 
        }
        .match-info { font-size: 0.8rem; color: #ffc107; margin-top: 6px; font-style: italic; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .search-result-count { color: #888; font-size: 0.9rem; margin-bottom: 16px; padding-bottom: 12px; border-bottom: 1px solid rgba(255,255,255,0.1); }
        
        .form-group { margin-bottom: 20px; }
        .field-hint { font-size: 0.8rem; color: #888; margin-top: 6px; font-style: italic; }
        
        label { display: block; margin-bottom: 8px; color: #4ecca3; font-weight: 500; font-size: 1rem; }
        
        input[type="text"], input[type="number"] {
            width: 100%;
            padding: 14px 16px;
            font-size: 1.1rem;
            border: 2px solid rgba(255,255,255,0.1);
            border-radius: 8px;
            background: rgba(0,0,0,0.3);
            color: #fff;
        }
        
        input:focus { outline: none; border-color: #4ecca3; }
        
        .track-list { background: rgba(0,0,0,0.2); border-radius: 8px; padding: 16px; max-height: 400px; overflow-y: auto; }
        .track-item { display: flex; align-items: center; padding: 10px 0; border-bottom: 1px solid rgba(255,255,255,0.05); gap: 8px; }
        .track-item:last-child { border-bottom: none; }
        .track-num { width: 30px; color: #4ecca3; font-weight: bold; flex-shrink: 0; }
        .track-fingerprinted {
            display: none;
            width: 24px;
            height: 24px;
            color: #4ecca3;
            flex-shrink: 0;
            animation: waveform-pulse 1.5s ease-in-out infinite;
        }
        .track-fingerprinted.show { display: flex; align-items: center; justify-content: center; }
        .track-fingerprinted svg { width: 18px; height: 18px; }
        @keyframes waveform-pulse {
            0%, 100% { opacity: 0.6; }
            50% { opacity: 1; }
        }
        .track-title { flex: 1; padding: 10px 12px; font-size: 1rem; border: 1px solid rgba(255,255,255,0.1); border-radius: 6px; background: rgba(0,0,0,0.3); color: #fff; min-width: 0; }
        .track-title:focus { outline: none; border-color: #4ecca3; }
        .track-title.fingerprinted { border-color: rgba(78,204,163,0.5); background: rgba(78,204,163,0.1); }
        
        .track-header { margin-bottom: 16px; }
        .track-header label { margin-bottom: 10px; }
        .track-instructions {
            display: flex;
            align-items: center;
            gap: 16px;
            margin-top: 12px;
            padding: 16px 20px;
            background: rgba(255,193,7,0.15);
            border: 2px solid rgba(255,193,7,0.4);
            border-radius: 10px;
            font-size: 1rem;
            color: #e0e0e0;
            font-weight: 500;
        }
        .instruction-item {
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .instruction-icon {
            width: 36px;
            height: 36px;
            border-radius: 6px;
            display: flex;
            align-items: center;
            justify-content: center;
            flex-shrink: 0;
        }
        .instruction-icon.fingerprint-icon {
            background: rgba(255,193,7,0.25);
            border: 2px solid rgba(255,193,7,0.6);
            color: #ffc107;
        }
        .instruction-icon svg { width: 20px; height: 20px; }
        .instruction-divider {
            color: #888;
            font-style: italic;
            font-weight: 400;
        }
        
        .fingerprint-btn {
            width: 32px;
            height: 32px;
            border-radius: 6px;
            border: 1px solid rgba(255,193,7,0.4);
            background: rgba(255,193,7,0.1);
            color: #ffc107;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            flex-shrink: 0;
            transition: all 0.2s;
        }
        .fingerprint-btn:hover { background: rgba(255,193,7,0.2); border-color: #ffc107; }
        .fingerprint-btn.loading { opacity: 0.5; cursor: wait; }
        .fingerprint-btn svg { width: 16px; height: 16px; }
        
        .track-fingerprint-results {
            background: rgba(0,0,0,0.9);
            border: 1px solid rgba(255,193,7,0.5);
            border-radius: 8px;
            padding: 12px;
            margin-top: 12px;
            max-height: 250px;
            overflow-y: auto;
        }
        .fingerprint-results-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 10px;
            padding-bottom: 8px;
            border-bottom: 1px solid rgba(255,255,255,0.1);
            color: #ffc107;
            font-weight: 500;
        }
        .fingerprint-close {
            background: none;
            border: none;
            color: #888;
            font-size: 1.5rem;
            cursor: pointer;
            line-height: 1;
        }
        .fingerprint-close:hover { color: #fff; }
        
        .fingerprint-result-item {
            padding: 10px 12px;
            background: rgba(255,255,255,0.05);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 6px;
            margin-bottom: 6px;
            cursor: pointer;
            transition: all 0.2s;
        }
        .fingerprint-result-item:hover {
            background: rgba(255,193,7,0.15);
            border-color: rgba(255,193,7,0.4);
        }
        .fingerprint-result-item:last-child { margin-bottom: 0; }
        .fingerprint-result-title { font-weight: 500; color: #fff; }
        .fingerprint-result-meta { font-size: 0.8rem; color: #888; margin-top: 2px; }
        .fingerprint-result-score {
            display: inline-block;
            font-size: 0.7rem;
            padding: 2px 6px;
            border-radius: 4px;
            background: rgba(78,204,163,0.2);
            color: #4ecca3;
            margin-left: 8px;
        }
        
        .art-container {
            display: flex;
            gap: 16px;
            align-items: flex-start;
        }
        .art-preview {
            width: 180px;
            height: 180px;
            background: rgba(0,0,0,0.3);
            border-radius: 10px;
            display: flex;
            align-items: center;
            justify-content: center;
            overflow: hidden;
            flex-shrink: 0;
            border: 2px solid rgba(255,255,255,0.1);
        }
        .art-preview.has-image { border-color: #4ecca3; }
        .art-preview img { width: 100%; height: 100%; object-fit: cover; }
        .art-placeholder { color: #666; font-size: 1rem; }
        .art-drop-zone {
            flex: 1;
            min-height: 180px;
            border: 2px dashed rgba(78, 204, 163, 0.4);
            border-radius: 10px;
            padding: 24px;
            text-align: center;
            cursor: pointer;
            transition: all 0.2s;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            gap: 10px;
            color: #888;
            font-size: 1rem;
        }
        .art-drop-zone:hover, .art-drop-zone.dragover {
            border-color: #4ecca3;
            background: rgba(78, 204, 163, 0.1);
            color: #4ecca3;
        }
        .art-drop-zone svg { width: 36px; height: 36px; }
        .art-hint { font-size: 0.85rem; opacity: 0.6; }
        
        .btn { display: inline-block; padding: 14px 28px; font-size: 1.1rem; font-weight: 600; border: none; border-radius: 8px; cursor: pointer; text-decoration: none; text-align: center; }
        .btn-primary { background: #4ecca3; color: #1a1a2e; }
        .btn-primary:hover { background: #3db892; }
        .btn-secondary { background: rgba(255,255,255,0.1); color: #e0e0e0; }
        .btn-secondary:hover { background: rgba(255,255,255,0.2); }
        .btn-small { padding: 10px 16px; font-size: 0.95rem; }
        .btn-wide { padding-left: 150px; padding-right: 150px; }
        .btn-medium { padding-left: 45px; padding-right: 45px; }
        .btn-danger { background: #ff5252; color: white; }
        .btn-danger:hover { background: #ff3333; }
        .btn-warning { background: #ffc107; color: #1a1a2e; }
        .btn-warning:hover { background: #e0a800; }
        .btn-info { background: #17a2b8; color: white; }
        .btn-info:hover { background: #138496; }
        
        .btn-group { display: flex; gap: 12px; margin-top: 24px; }
        
        .message { padding: 16px; border-radius: 8px; margin-bottom: 20px; text-align: center; }
        .message.success { background: rgba(78, 204, 163, 0.2); border: 1px solid #4ecca3; color: #4ecca3; }
        .message.error { background: rgba(255, 82, 82, 0.2); border: 1px solid #ff5252; color: #ff5252; }
        .message.info { background: rgba(23, 162, 184, 0.2); border: 1px solid #17a2b8; color: #17a2b8; }
        
        .empty-state { text-align: center; padding: 60px 20px; color: #888; }
        .empty-state h3 { margin-bottom: 10px; }
        
        .back-link { display: inline-flex; align-items: center; color: #4ecca3; text-decoration: none; margin-bottom: 20px; font-size: 1rem; }
        .back-link:hover { text-decoration: underline; }
        
        .warning-banner { background: rgba(255, 193, 7, 0.2); border: 1px solid #ffc107; color: #ffc107; padding: 12px 16px; border-radius: 8px; margin-bottom: 20px; display: flex; align-items: center; gap: 10px; }
        .warning-banner svg { flex-shrink: 0; }
        
        /* Fetch/Identify Status */
        .identify-status {
            padding: 12px;
            border-radius: 6px;
            margin-top: 12px;
            font-size: 0.9rem;
        }
        .identify-status.loading { background: rgba(255,255,255,0.1); color: #888; }
        .identify-status.success { background: rgba(78, 204, 163, 0.2); color: #4ecca3; }
        .identify-status.error { background: rgba(255, 82, 82, 0.2); color: #ff5252; }
        
        .identify-results {
            margin-top: 12px;
        }
        
        /* Fetch section */
        .fetch-section {
            background: rgba(23, 162, 184, 0.1);
            border: 1px solid rgba(23, 162, 184, 0.3);
            border-radius: 8px;
            padding: 16px;
            margin-bottom: 20px;
        }
        
        .fetch-section-prominent {
            background: rgba(23, 162, 184, 0.15);
            border: 2px solid rgba(23, 162, 184, 0.4);
            padding: 20px;
        }
        
        .fetch-header-static {
            display: flex;
            align-items: center;
            gap: 12px;
            color: #17a2b8;
            font-weight: 600;
            font-size: 1.1rem;
            margin-bottom: 12px;
        }
        
        .fetch-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            cursor: pointer;
            color: #17a2b8;
            font-weight: 500;
        }
        
        .fetch-header svg { transition: transform 0.2s; }
        .fetch-header.open svg { transform: rotate(180deg); }
        
        .fetch-content {
            display: none;
            margin-top: 16px;
            padding-top: 16px;
            border-top: 1px solid rgba(23, 162, 184, 0.2);
        }
        
        .fetch-content.show { display: block; }
        
        .fetch-row {
            display: flex;
            gap: 12px;
            margin-bottom: 12px;
        }
        
        .fetch-row .form-group { flex: 1; margin-bottom: 0; }
        .fetch-row input { padding: 12px; font-size: 1rem; }
        
        
        .history-item { background: rgba(0,0,0,0.2); border-radius: 8px; padding: 16px; margin-bottom: 12px; border-left: 4px solid #4ecca3; }
        .history-item.undone { opacity: 0.5; border-left-color: #888; }
        .history-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 8px; }
        .history-title { font-weight: 600; color: #4ecca3; }
        .history-time { color: #888; font-size: 0.85rem; }
        .history-details { color: #aaa; font-size: 0.9rem; }
        
        .modal-overlay { display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.7); z-index: 1000; align-items: center; justify-content: center; }
        .modal-overlay.show { display: flex; }
        .modal { background: #1a1a2e; border-radius: 12px; padding: 24px; max-width: 500px; width: 90%; border: 1px solid rgba(255,255,255,0.1); }
        .modal h3 { color: #ffc107; margin-bottom: 16px; display: flex; align-items: center; gap: 10px; }
        .modal p { margin-bottom: 20px; line-height: 1.5; }
        .modal .btn-group { margin-top: 0; }
        
        .undo-float { position: fixed; bottom: 20px; right: 20px; z-index: 100; }
        
        .spinner { display: inline-block; width: 16px; height: 16px; border: 2px solid rgba(255,255,255,0.3); border-top-color: #fff; border-radius: 50%; animation: spin 0.8s linear infinite; margin-right: 8px; }
        @keyframes spin { to { transform: rotate(360deg); } }
        
        /* Audio player styles */
        .play-btn {
            width: 32px;
            height: 32px;
            border-radius: 50%;
            border: none;
            background: rgba(78, 204, 163, 0.2);
            color: #4ecca3;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            flex-shrink: 0;
            margin-right: 8px;
            transition: all 0.2s;
        }
        .play-btn:hover { background: rgba(78, 204, 163, 0.4); transform: scale(1.1); }
        .play-btn.playing { background: #4ecca3; color: #1a1a2e; }
        .play-btn svg { width: 16px; height: 16px; }
        
        .now-playing {
            display: flex;
            align-items: center;
            gap: 12px;
            margin-top: 16px;
            padding: 12px 16px;
            background: rgba(78, 204, 163, 0.15);
            border: 1px solid rgba(78, 204, 163, 0.3);
            border-radius: 8px;
        }
        
        .stop-btn {
            width: 32px;
            height: 32px;
            border-radius: 50%;
            border: none;
            background: #ff5252;
            color: white;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            flex-shrink: 0;
        }
        .stop-btn:hover { background: #ff3333; }
        .stop-btn svg { width: 14px; height: 14px; }
        
        #nowPlayingText { flex: 1; font-size: 0.9rem; color: #4ecca3; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        
        .audio-seek {
            width: 150px;
            height: 6px;
            -webkit-appearance: none;
            appearance: none;
            background: rgba(255,255,255,0.2);
            border-radius: 3px;
            cursor: pointer;
        }
        .audio-seek::-webkit-slider-thumb {
            -webkit-appearance: none;
            width: 14px;
            height: 14px;
            background: #4ecca3;
            border-radius: 50%;
            cursor: pointer;
        }
        .audio-seek::-moz-range-thumb {
            width: 14px;
            height: 14px;
            background: #4ecca3;
            border-radius: 50%;
            cursor: pointer;
            border: none;
        }
        
    </style>
</head>
<body>
    <div class="container">
        <h1>Pi Music Manager</h1>
        
        <div class="search-container">
            <form action="/search" method="GET" class="search-box">
                <input type="text" name="q" class="search-input" placeholder="Search artists, albums, or tracks..." value="{{ search_query or '' }}" autocomplete="off">
                <button type="submit" class="search-btn">Search</button>
                {% if search_query %}
                <a href="/" class="search-clear">Clear</a>
                {% endif %}
                <button type="button" class="refresh-btn" onclick="location.reload()" title="Refresh library"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M17.65 6.35A7.958 7.958 0 0012 4c-4.42 0-7.99 3.58-7.99 8s3.57 8 7.99 8c3.73 0 6.84-2.55 7.73-6h-2.08A5.99 5.99 0 0112 18c-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z"/></svg></button>
            </form>
        </div>
        
        {% if message %}
        <div class="message {{ message_type }}">{{ message }}</div>
        {% endif %}
        
        {% if album %}
        <!-- EDIT ALBUM VIEW -->
        <a href="{{ back_url or '/' }}" class="back-link">&larr; Back</a>
        
        {% if is_existing %}
        <div class="warning-banner">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="currentColor"><path d="M1 21h22L12 2 1 21zm12-3h-2v-2h2v2zm0-4h-2v-4h2v4z"/></svg>
            <span><strong>Warning:</strong> You are editing an existing album. Changes will overwrite current metadata.</span>
        </div>
        {% endif %}
        
        <!-- MUSICBRAINZ SEARCH SECTION -->
        {% if not is_existing %}
        <div class="fetch-section fetch-section-prominent">
            <div class="fetch-header-static">
                <svg width="24" height="24" viewBox="0 0 24 24" fill="currentColor"><path d="M15.5 14h-.79l-.28-.27A6.471 6.471 0 0016 9.5 6.5 6.5 0 109.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"/></svg>
                <span>Search MusicBrainz for Album Info</span>
            </div>
            <p style="color:#888;font-size:0.9rem;margin-bottom:12px;">Search by artist and album name, or use the fingerprint button on each track below.</p>
            <div class="fetch-row">
                <div class="form-group">
                    <input type="text" id="fetchArtist" placeholder="Artist name">
                </div>
                <div class="form-group">
                    <input type="text" id="fetchAlbum" placeholder="Album name">
                </div>
            </div>
            <button type="button" class="btn btn-info" onclick="fetchMetadata()" id="fetchBtn">
                Search MusicBrainz
            </button>
            <div class="identify-status" id="fetchStatus" style="display:none;"></div>
            <div class="identify-results" id="fetchResults" style="display:none;"></div>
        </div>
        {% else %}
        <div class="fetch-section">
            <div class="fetch-header" onclick="toggleFetch()">
                <span>Search MusicBrainz</span>
                <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><path d="M7.41 8.59L12 13.17l4.59-4.58L18 10l-6 6-6-6 1.41-1.41z"/></svg>
            </div>
            <div class="fetch-content" id="fetchContent">
                <div class="fetch-row">
                    <div class="form-group" style="margin-bottom:8px;">
                        <input type="text" id="fetchArtist" placeholder="Artist">
                    </div>
                    <div class="form-group" style="margin-bottom:8px;">
                        <input type="text" id="fetchAlbum" placeholder="Album">
                    </div>
                </div>
                <button type="button" class="btn btn-info btn-small" onclick="fetchMetadata()" id="fetchBtn">
                    Search
                </button>
                <div class="identify-status" id="fetchStatus" style="display:none;"></div>
                <div class="identify-results" id="fetchResults" style="display:none;"></div>
            </div>
        </div>
        {% endif %}
        
        <form id="tagForm" method="POST" enctype="multipart/form-data">
            <input type="hidden" name="folder" value="{{ album.folder }}">
            <input type="hidden" name="original_path" value="{{ album.original_path }}">
            <input type="hidden" name="is_existing" value="{{ is_existing }}">
            
            <div class="card">
                <div class="form-group">
                    <label for="artist">Artist Name</label>
                    <input type="text" id="artist" name="artist" value="{{ album.artist }}" placeholder="Enter artist name" required>
                    <div class="field-hint">Tip: Use "Various Artists" for compilations - tracks will be parsed as "Artist - Title"</div>
                </div>
                
                <div class="form-group">
                    <label for="album_name">Album Name</label>
                    <input type="text" id="album_name" name="album_name" value="{{ album.album }}" placeholder="Enter album name" required>
                </div>
                
                <div class="form-group" style="display: flex; gap: 20px;">
                    <div style="flex: 1;">
                        <label for="year">Year</label>
                        <input type="number" id="year" name="year" value="{{ album.year }}" placeholder="2024" min="1900" max="2099">
                    </div>
                    <div style="flex: 1;">
                        <label for="genre">Genre</label>
                        <input type="text" id="genre" name="genre" value="{{ album.genre }}" placeholder="Rock, Jazz, etc.">
                    </div>
                </div>
                
                <div class="form-group" style="display: flex; gap: 20px;">
                    <div style="flex: 1;">
                        <label for="disc_number">Disc Number</label>
                        <input type="number" id="disc_number" name="disc_number" value="{{ album.disc_number }}" placeholder="1" min="1" max="99">
                    </div>
                    <div style="flex: 1;">
                        <label for="disc_total">Total Discs</label>
                        <input type="number" id="disc_total" name="disc_total" value="{{ album.disc_total }}" placeholder="1" min="1" max="99">
                    </div>
                </div>
            </div>
            
            <div class="card">
                <label>Album Art</label>
                <div class="art-container">
                    <div class="art-preview" id="artPreview">
                        <span class="art-placeholder">No image</span>
                    </div>
                    <div class="art-drop-zone" id="dropZone">
                        <input type="file" id="artFile" name="art_file" accept="image/*" style="display:none;">
                        <input type="hidden" id="artUrl" name="art_url">
                        <svg width="24" height="24" viewBox="0 0 24 24" fill="currentColor" style="opacity:0.5;"><path d="M19.35 10.04C18.67 6.59 15.64 4 12 4 9.11 4 6.6 5.64 5.35 8.04 2.34 8.36 0 10.91 0 14c0 3.31 2.69 6 6 6h13c2.76 0 5-2.24 5-5 0-2.64-2.05-4.78-4.65-4.96zM14 13v4h-4v-4H7l5-5 5 5h-3z"/></svg>
                        <span>Drop image or click to browse</span>
                        <span class="art-hint">or paste image URL anywhere</span>
                    </div>
                </div>
            </div>
            
            <div class="card">
                <div class="track-header">
                    <label>Tracks ({{ album.tracks|length }} total)</label>
                    <div class="track-instructions">
                        <span class="instruction-item">
                            <span class="instruction-icon fingerprint-icon">
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M12 2C8.13 2 5 5.13 5 9c0 2.38 1.19 4.47 3 5.74V22"/><path d="M12 2c3.87 0 7 3.13 7 9 0 3-1 5.5-2.5 7.5"/><path d="M12 6c-1.66 0-3 1.34-3 3v4"/><path d="M12 6c1.66 0 3 1.34 3 3 0 2.5-.5 4.5-1.5 6.5"/><path d="M12 10v8"/></svg>
                            </span>
                            Auto-identify track from audio fingerprint
                        </span>
                        <span class="instruction-divider">or</span>
                        <span class="instruction-item">Type title manually (use "Artist - Title" for compilations)</span>
                    </div>
                </div>
                <div class="track-list">
                    {% for track in album.tracks %}
                    <div class="track-item" data-track-path="{{ track.path }}" data-track-num="{{ track.num }}">
                        <button type="button" class="play-btn" onclick="playTrack(this.closest('.track-item').dataset.trackPath, this)" title="Play preview">
                            <svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>
                        </button>
                        <span class="track-num">{{ track.num }}</span>
                        <span class="track-fingerprinted" title="Identified via audio fingerprint">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 2C8.13 2 5 5.13 5 9c0 2.38 1.19 4.47 3 5.74V22"/><path d="M12 2c3.87 0 7 3.13 7 9 0 3-1 5.5-2.5 7.5"/><path d="M12 6c-1.66 0-3 1.34-3 3v4"/><path d="M12 6c1.66 0 3 1.34 3 3 0 2.5-.5 4.5-1.5 6.5"/><path d="M12 10v8"/></svg>
                        </span>
                        <input type="text" class="track-title" name="track_{{ track.num }}" value="{{ track.title }}" placeholder="Track {{ track.num }} title">
                        <button type="button" class="fingerprint-btn" onclick="fingerprintTrack(this)" title="Identify track from audio fingerprint">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 2C8.13 2 5 5.13 5 9c0 2.38 1.19 4.47 3 5.74V22"/><path d="M12 2c3.87 0 7 3.13 7 9 0 3-1 5.5-2.5 7.5"/><path d="M12 6c-1.66 0-3 1.34-3 3v4"/><path d="M12 6c1.66 0 3 1.34 3 3 0 2.5-.5 4.5-1.5 6.5"/><path d="M12 10v8"/></svg>
                        </button>
                    </div>
                    {% endfor %}
                </div>
                
                <!-- Fingerprint results dropdown (hidden by default) -->
                <div id="trackFingerprintResults" class="track-fingerprint-results" style="display:none;">
                    <div class="fingerprint-results-header">
                        <span>Select track title:</span>
                        <button type="button" class="fingerprint-close" onclick="closeFingerprintResults()">&times;</button>
                    </div>
                    <div class="fingerprint-results-list" id="fingerprintResultsList"></div>
                </div>
                <audio id="audioPlayer" style="display:none;"></audio>
                <div id="nowPlaying" class="now-playing" style="display:none;">
                    <button type="button" class="stop-btn" onclick="stopAudio()">
                        <svg viewBox="0 0 24 24" fill="currentColor"><path d="M6 6h12v12H6z"/></svg>
                    </button>
                    <span id="nowPlayingText">Playing...</span>
                    <input type="range" id="audioSeek" min="0" max="100" value="0" class="audio-seek">
                </div>
            </div>
            
            <div class="btn-group" style="justify-content: space-between; align-items: center;">
                <div style="display: flex; gap: 12px; align-items: center;">
                    <a href="{{ back_url or '/' }}" class="btn btn-secondary">Cancel</a>
                    {% if is_existing %}
                    <button type="button" class="btn btn-warning btn-wide" onclick="showConfirmModal()">Save Changes</button>
                    {% else %}
                    <button type="submit" class="btn btn-primary btn-wide" id="saveBtn">Save &amp; Organize</button>
                    {% endif %}
                </div>
                <button type="button" class="btn btn-danger btn-medium" onclick="showDeleteModal()">Delete Album</button>
            </div>
        </form>
        
        {% elif view == 'search' %}
        <div class="tabs">
            <a href="/" class="tab">Unknown Albums{% if counts and counts.unknown %} ({{ counts.unknown }}){% endif %}</a>
            <a href="/browse" class="tab">All Albums{% if counts and counts.total %} ({{ counts.total }}){% endif %}</a>
            <a href="/history" class="tab">Edit History</a>
            <a href="/debug" class="tab">Debug ARM</a>
        </div>
        <div class="card">
            {% if search_results %}
            <div class="search-result-count">Found {{ search_results|length }} result(s) for "{{ search_query }}"</div>
            <ul class="album-list">
                {% for album in search_results %}
                <li class="album-item album-item-with-art {% if not album.is_unknown %}warning{% endif %} {% if album.is_unknown %}album-unknown{% else %}album-clickable{% endif %}" data-path="{{ album.path }}" data-artist="{{ album.artist }}" data-album="{{ album.album_name }}">
                    <div class="album-thumb" data-path="{{ album.path }}">
                        <svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 3v10.55c-.59-.34-1.27-.55-2-.55-2.21 0-4 1.79-4 4s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z"/></svg>
                    </div>
                    <div class="album-info">
                        <span class="album-name">{{ album.album_name }}</span>
                        <div class="album-artist">{{ album.artist }}</div>
                        {% if album.matching_tracks %}<div class="match-info">Matching tracks: {{ album.matching_tracks|join(', ') }}{% if album.matching_tracks|length >= 3 %}...{% endif %}</div>{% endif %}
                    </div>
                    <span class="track-count">{{ album.track_count }} tracks</span>
                </li>
                {% endfor %}
            </ul>
            {% else %}
            <div class="empty-state"><h3>No results found</h3><p>Try a different search term.</p></div>
            {% endif %}
        </div>
        
        {% elif view == 'history' %}
        <div class="tabs">
            <a href="/" class="tab">Unknown Albums{% if counts and counts.unknown %} ({{ counts.unknown }}){% endif %}</a>
            <a href="/browse" class="tab">All Albums{% if counts and counts.total %} ({{ counts.total }}){% endif %}</a>
            <a href="/history" class="tab active">Edit History</a>
            <a href="/debug" class="tab">Debug ARM</a>
        </div>
        <div class="card">
            {% if history %}
            {% for item in history|reverse %}
            <div class="history-item {% if item.undone %}undone{% endif %}">
                <div class="history-header">
                    <span class="history-title">{{ item.artist }} - {{ item.album }}</span>
                    <span class="history-time">{{ item.timestamp[:16].replace('T', ' ') }}</span>
                </div>
                <div class="history-details">{{ item.action }}{% if item.undone %}<br><em>(Undone)</em>{% endif %}</div>
            </div>
            {% endfor %}
            {% else %}
            <div class="empty-state"><h3>No edit history</h3><p>Your tagging history will appear here.</p></div>
            {% endif %}
        </div>
        
        {% elif view == 'browse' %}
        <div class="tabs">
            <a href="/" class="tab">Unknown Albums{% if counts and counts.unknown %} ({{ counts.unknown }}){% endif %}</a>
            <a href="/browse" class="tab active">All Albums{% if counts and counts.total %} ({{ counts.total }}){% endif %}</a>
            <a href="/history" class="tab">Edit History</a>
            <a href="/debug" class="tab">Debug ARM</a>
        </div>
        <div class="card">
            {% if albums %}
            <ul class="album-list">
                {% for album in albums %}
                <li class="album-item album-item-with-art album-clickable" data-path="{{ album.path }}" data-artist="{{ album.artist }}" data-album="{{ album.album_name }}">
                    <div class="album-thumb" data-path="{{ album.path }}">
                        <svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 3v10.55c-.59-.34-1.27-.55-2-.55-2.21 0-4 1.79-4 4s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z"/></svg>
                    </div>
                    <div class="album-info">
                        <span class="album-name">{{ album.album_name }}</span>
                        <div class="album-artist">{{ album.artist }}</div>
                    </div>
                    <span class="track-count">{{ album.track_count }} tracks</span>
                </li>
                {% endfor %}
            </ul>
            {% else %}
            <div class="empty-state"><h3>No albums found</h3><p>Your music library is empty.</p></div>
            {% endif %}
        </div>
        
        {% elif view == 'debug' %}
        <div class="tabs">
            <a href="/" class="tab">Unknown Albums{% if counts and counts.unknown %} ({{ counts.unknown }}){% endif %}</a>
            <a href="/browse" class="tab">All Albums{% if counts and counts.total %} ({{ counts.total }}){% endif %}</a>
            <a href="/history" class="tab">Edit History</a>
            <a href="/debug" class="tab active">Debug ARM</a>
        </div>
        
        <!-- Disk Space & Quick Actions -->
        <div class="card" style="margin-bottom: 20px;">
            <div style="display: flex; gap: 20px; flex-wrap: wrap;">
                <!-- Disk Usage -->
                <div style="flex: 1; min-width: 280px;">
                    <div style="display: flex; align-items: center; margin-bottom: 12px;">
                        <svg viewBox="0 0 24 24" fill="#4ecca3" width="24" height="24" style="margin-right: 10px;"><path d="M20 6H12l-2-2H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2z"/></svg>
                        <span style="font-weight: 600; color: #4ecca3;">Disk Usage</span>
                    </div>
                    <div style="background: rgba(0,0,0,0.3); border-radius: 8px; padding: 16px;">
                        <div style="display: flex; justify-content: space-between; margin-bottom: 8px;">
                            <span>Music Library</span>
                            <span style="color: #4ecca3; font-weight: 600;">{{ disk_usage.music_size_human }}</span>
                        </div>
                        <div style="display: flex; justify-content: space-between; margin-bottom: 8px;">
                            <span>System Files</span>
                            <span style="color: #888;">{{ disk_usage.system_size_human }}</span>
                        </div>
                        <div style="border-top: 1px solid rgba(255,255,255,0.1); padding-top: 8px; margin-top: 8px; display: flex; justify-content: space-between;">
                            <span>Free Space</span>
                            <span style="color: {% if disk_usage.usage_percent > 90 %}#f44336{% elif disk_usage.usage_percent > 75 %}#ffc107{% else %}#4ecca3{% endif %}; font-weight: 600;">{{ disk_usage.free_space_human }}</span>
                        </div>
                        <div style="margin-top: 12px; background: rgba(255,255,255,0.1); border-radius: 4px; height: 8px; overflow: hidden;">
                            <div style="background: {% if disk_usage.usage_percent > 90 %}#f44336{% elif disk_usage.usage_percent > 75 %}#ffc107{% else %}#4ecca3{% endif %}; height: 100%; width: {{ disk_usage.usage_percent }}%;"></div>
                        </div>
                        <div style="text-align: center; margin-top: 6px; font-size: 0.85rem; color: #888;">{{ disk_usage.usage_percent }}% used</div>
                    </div>
                </div>
                
                <!-- Quick Actions -->
                <div style="flex: 1; min-width: 200px;">
                    <div style="display: flex; align-items: center; margin-bottom: 12px;">
                        <svg viewBox="0 0 24 24" fill="#4ecca3" width="24" height="24" style="margin-right: 10px;"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/></svg>
                        <span style="font-weight: 600; color: #4ecca3;">Quick Actions</span>
                    </div>
                    <div style="display: flex; flex-direction: column; gap: 10px;">
                        <button onclick="ejectCD()" style="display: flex; align-items: center; justify-content: center; gap: 8px; padding: 12px 20px; background: rgba(78, 204, 163, 0.2); border: 1px solid rgba(78, 204, 163, 0.4); border-radius: 8px; color: #4ecca3; font-size: 1rem; font-weight: 500; cursor: pointer; transition: all 0.2s;">
                            <svg viewBox="0 0 24 24" fill="currentColor" width="18" height="18"><path d="M12 5l-7 7h14l-7-7zm-7 9v2h14v-2H5z"/></svg>
                            Eject CD
                        </button>
                        <button onclick="location.reload()" style="display: flex; align-items: center; justify-content: center; gap: 8px; padding: 12px 20px; background: rgba(78, 204, 163, 0.2); border: 1px solid rgba(78, 204, 163, 0.4); border-radius: 8px; color: #4ecca3; font-size: 1rem; font-weight: 500; cursor: pointer; transition: all 0.2s;">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="18" height="18"><path d="M23 4v6h-6M1 20v-6h6"/><path d="M3.51 9a9 9 0 0114.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0020.49 15"/></svg>
                            Refresh Status
                        </button>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- System Diagnostics -->
        <div class="card" style="margin-bottom: 20px;">
            <div class="card-header">
                <span class="card-title">System Diagnostics</span>
            </div>
            
            {% if diagnostics.rip_in_progress %}
            <div class="debug-warning" style="background: rgba(255, 193, 7, 0.2); border: 1px solid rgba(255, 193, 7, 0.5); border-radius: 8px; padding: 16px; margin-bottom: 20px;">
                <strong style="color: #ffc107;">Rip In Progress</strong>
                <p style="margin-top: 8px; color: #ccc;">A CD rip appears to be running. Do not clean up until it completes.</p>
            </div>
            {% endif %}
            
            <div class="debug-checks" style="margin-bottom: 24px;">
                {% for check in diagnostics.checks %}
                <div class="debug-check-item" style="display: flex; align-items: center; padding: 12px 0; border-bottom: 1px solid rgba(255,255,255,0.05);">
                    <span class="debug-status" style="width: 24px; height: 24px; margin-right: 12px; display: flex; align-items: center; justify-content: center;">
                        {% if check.status == 'ok' %}
                        <svg viewBox="0 0 24 24" fill="#4ecca3" width="20" height="20"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/></svg>
                        {% elif check.status == 'warning' %}
                        <svg viewBox="0 0 24 24" fill="#ffc107" width="20" height="20"><path d="M1 21h22L12 2 1 21zm12-3h-2v-2h2v2zm0-4h-2v-4h2v4z"/></svg>
                        {% elif check.status == 'error' %}
                        <svg viewBox="0 0 24 24" fill="#f44336" width="20" height="20"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z"/></svg>
                        {% else %}
                        <svg viewBox="0 0 24 24" fill="#2196f3" width="20" height="20"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/></svg>
                        {% endif %}
                    </span>
                    <span class="debug-check-name" style="flex: 1; font-weight: 500;">{{ check.name }}</span>
                    <span class="debug-check-message" style="color: #888;">{{ check.message }}</span>
                </div>
                {% endfor %}
            </div>
            
            {% if diagnostics.abcde_folders %}
            <div class="debug-section" style="margin-bottom: 20px;">
                <h4 style="color: #ffc107; margin-bottom: 12px;">Leftover Temp Folders</h4>
                <div style="background: rgba(0,0,0,0.2); border-radius: 8px; padding: 12px;">
                    {% for folder in diagnostics.abcde_folders %}
                    <div style="padding: 8px 0; border-bottom: 1px solid rgba(255,255,255,0.05); font-family: monospace; font-size: 0.9rem;">
                        {{ folder.name }} <span style="color: #888;">({{ folder.size_mb }} MB)</span>
                    </div>
                    {% endfor %}
                </div>
            </div>
            {% endif %}
            
            {% if diagnostics.leftover_wavs %}
            <div class="debug-section" style="margin-bottom: 20px;">
                <h4 style="color: #ffc107; margin-bottom: 12px;">Leftover WAV Files</h4>
                <div style="background: rgba(0,0,0,0.2); border-radius: 8px; padding: 12px; max-height: 150px; overflow-y: auto;">
                    {% for wav in diagnostics.leftover_wavs %}
                    <div style="padding: 4px 0; font-family: monospace; font-size: 0.85rem; color: #888;">{{ wav }}</div>
                    {% endfor %}
                </div>
            </div>
            {% endif %}
            
            <div class="debug-actions" style="display: flex; gap: 12px; padding-top: 16px; border-top: 1px solid rgba(255,255,255,0.1);">
                {% if diagnostics.issues_found > 0 and not diagnostics.rip_in_progress %}
                <button class="btn btn-warning" onclick="confirmCleanup()">Reset ARM</button>
                {% elif diagnostics.rip_in_progress %}
                <button class="btn btn-secondary" disabled>Cannot Reset During Rip</button>
                {% else %}
                <span style="color: #4ecca3; padding: 12px 0;">All checks passed - no reset needed</span>
                {% endif %}
            </div>
        </div>
        
        <!-- Log Viewer -->
        <div class="card">
            <div class="card-header">
                <span class="card-title">Recent Logs</span>
                <select id="logSelect" onchange="loadLog()" style="background: rgba(0,0,0,0.3); border: 1px solid rgba(255,255,255,0.2); border-radius: 6px; padding: 8px 12px; color: #fff; font-size: 0.9rem;">
                    <option value="">Select a log file...</option>
                    {% for log in recent_logs %}
                    <option value="{{ log.name }}">{{ log.name }} ({{ log.modified }})</option>
                    {% endfor %}
                </select>
            </div>
            <div id="logContent" style="background: rgba(0,0,0,0.3); border-radius: 8px; padding: 16px; min-height: 200px; max-height: 400px; overflow-y: auto; font-family: 'Monaco', 'Menlo', 'Consolas', monospace; font-size: 0.8rem; line-height: 1.5; white-space: pre-wrap; word-break: break-all; color: #aaa;">
                Select a log file above to view its contents...
            </div>
        </div>
        
        <script>
        function confirmCleanup() {
            if (confirm('This will:\\n- Delete leftover temp folders and WAV files\\n- Reset any stuck/zombie jobs in the database\\n\\nAre you sure?')) {
                fetch('/api/debug-clean', { method: 'POST' })
                    .then(r => r.json())
                    .then(data => {
                        if (data.success) {
                            alert(data.message + '\\n\\nPage will refresh.');
                            location.reload();
                        } else {
                            alert('Error: ' + data.error);
                        }
                    })
                    .catch(e => alert('Error: ' + e));
            }
        }
        
        function ejectCD() {
            fetch('/api/eject-cd', { method: 'POST' })
                .then(r => r.json())
                .then(data => {
                    if (data.success) {
                        alert('CD ejected successfully!');
                    } else {
                        alert('Eject failed: ' + data.error);
                    }
                })
                .catch(e => alert('Error: ' + e));
        }
        
        function loadLog() {
            const select = document.getElementById('logSelect');
            const logName = select.value;
            const content = document.getElementById('logContent');
            
            if (!logName) {
                content.textContent = 'Select a log file above to view its contents...';
                return;
            }
            
            content.textContent = 'Loading...';
            fetch('/api/logs/' + encodeURIComponent(logName))
                .then(r => r.json())
                .then(data => {
                    if (data.success) {
                        content.textContent = data.content || '(empty log file)';
                        content.scrollTop = content.scrollHeight;
                    } else {
                        content.textContent = 'Error: ' + data.error;
                    }
                })
                .catch(e => {
                    content.textContent = 'Error loading log: ' + e;
                });
        }
        </script>
        
        {% else %}
        <div class="tabs">
            <a href="/" class="tab active">Unknown Albums{% if counts and counts.unknown %} ({{ counts.unknown }}){% endif %}</a>
            <a href="/browse" class="tab">All Albums{% if counts and counts.total %} ({{ counts.total }}){% endif %}</a>
            <a href="/history" class="tab">Edit History</a>
            <a href="/debug" class="tab">Debug ARM</a>
        </div>
        <div class="card">
            {% if albums %}
            <ul class="album-list">
                {% for album in albums %}
                <li class="album-item album-unknown" data-album="{{ album.folder }}">
                    <span class="album-name">{{ album.name }}</span>
                    <span class="track-count">{{ album.track_count }} tracks</span>
                </li>
                {% endfor %}
            </ul>
            {% else %}
            <div class="empty-state"><h3>All albums are tagged!</h3><p>No unknown albums to process.</p></div>
            {% endif %}
        </div>
        {% endif %}
    </div>
    
    {% if can_undo %}
    <div class="undo-float">
        <form action="/undo" method="POST" style="display:inline;">
            <button type="submit" class="btn btn-danger">Undo Last Edit</button>
        </form>
    </div>
    {% endif %}
    
    <div class="modal-overlay" id="confirmModal">
        <div class="modal">
            <h3><svg width="24" height="24" viewBox="0 0 24 24" fill="currentColor"><path d="M1 21h22L12 2 1 21zm12-3h-2v-2h2v2zm0-4h-2v-4h2v4z"/></svg>Confirm Changes</h3>
            <p>You are about to modify an existing album's metadata. This will overwrite the current tags and may rename/move files.</p>
            <p><strong>Are you sure you want to continue?</strong></p>
            <div class="btn-group">
                <button type="button" class="btn btn-secondary" onclick="hideConfirmModal()">Cancel</button>
                <button type="button" class="btn btn-warning" onclick="submitForm()">Yes, Save Changes</button>
            </div>
        </div>
    </div>
    
    <div class="modal-overlay" id="browseConfirmModal">
        <div class="modal">
            <h3><svg width="24" height="24" viewBox="0 0 24 24" fill="currentColor"><path d="M1 21h22L12 2 1 21zm12-3h-2v-2h2v2zm0-4h-2v-4h2v4z"/></svg>Edit Existing Album?</h3>
            <p id="browseConfirmText">You are about to edit an existing album.</p>
            <p><strong>Changes will overwrite current metadata.</strong></p>
            <div class="btn-group">
                <button type="button" class="btn btn-secondary" onclick="hideBrowseModal()">Cancel</button>
                <a href="#" id="browseConfirmLink" class="btn btn-warning">Edit Album</a>
            </div>
        </div>
    </div>
    
    <div class="modal-overlay" id="deleteModal">
        <div class="modal">
            <h3 style="color: #ff5252;"><svg width="24" height="24" viewBox="0 0 24 24" fill="currentColor"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>Delete Album?</h3>
            <p>This will permanently delete this album and all its tracks.</p>
            <p><strong>This action cannot be undone unless you keep a backup.</strong></p>
            <div style="margin: 16px 0;">
                <label style="display: flex; align-items: center; gap: 8px; cursor: pointer; color: #e0e0e0;">
                    <input type="checkbox" id="keepBackup" checked style="width: 18px; height: 18px;">
                    Keep backup (allows undo)
                </label>
            </div>
            <div class="btn-group">
                <button type="button" class="btn btn-secondary" onclick="hideDeleteModal()">Cancel</button>
                <button type="button" class="btn btn-danger" onclick="confirmDelete()">Delete Album</button>
            </div>
        </div>
    </div>
    
    <form id="deleteForm" action="/delete" method="POST" style="display:none;">
        <input type="hidden" name="path" id="deletePath">
        <input type="hidden" name="keep_backup" id="deleteKeepBackup">
    </form>
    
    <script>
        // Auto-identify functions
        function toggleFetch() {
            const content = document.getElementById('fetchContent');
            const header = document.querySelector('.fetch-header');
            content.classList.toggle('show');
            header.classList.toggle('open');
        }
        
        // Per-track fingerprinting
        let currentFingerprintTrackNum = null;
        
        async function fingerprintTrack(btn) {
            const trackItem = btn.closest('.track-item');
            const trackPath = trackItem.getAttribute('data-track-path');
            const trackNum = trackItem.getAttribute('data-track-num');
            const resultsContainer = document.getElementById('trackFingerprintResults');
            const resultsList = document.getElementById('fingerprintResultsList');
            
            if (!trackPath) {
                alert('Track path not found');
                return;
            }
            
            // Store which track we're fingerprinting
            currentFingerprintTrackNum = trackNum;
            
            // Show loading state
            btn.classList.add('loading');
            btn.disabled = true;
            
            try {
                const response = await fetch('/api/fingerprint-track?path=' + encodeURIComponent(trackPath));
                const data = await response.json();
                
                if (data.success && data.matches && data.matches.length > 0) {
                    // Build results list
                    let html = '';
                    // Check if album artist suggests this is a compilation
                    var albumArtist = (document.getElementById('artist').value || '').toLowerCase().trim();
                    var compilationKeywords = ['various artists', 'various', 'va', 'soundtrack', 'ost', 'compilation', 'sampler'];
                    var isCompilation = compilationKeywords.some(function(kw) { return albumArtist.indexOf(kw) !== -1; });
                    
                    data.matches.forEach(function(m) {
                        // Only format as "Artist - Title" for compilations
                        var displayTitle = isCompilation && m.artist ? (m.artist + ' - ' + m.title) : m.title;
                        html += '<div class="fingerprint-result-item" onclick="applyTrackFingerprint(\'' + escapeAttr(displayTitle) + '\')">';
                        html += '<span class="fingerprint-result-title">' + escapeHtml(m.title) + '</span>';
                        html += '<span class="fingerprint-result-score">' + m.score + '%</span>';
                        html += '<div class="fingerprint-result-meta">' + escapeHtml(m.artist);
                        if (m.album) html += ' - ' + escapeHtml(m.album);
                        html += '</div>';
                        html += '</div>';
                    });
                    
                    resultsList.innerHTML = html;
                    resultsContainer.style.display = 'block';
                    
                    // Scroll results into view
                    resultsContainer.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
                } else {
                    alert(data.error || 'No matches found for this track');
                }
            } catch (e) {
                alert('Error fingerprinting track: ' + e.message);
            }
            
            // Reset button
            btn.classList.remove('loading');
            btn.disabled = false;
        }
        
        function applyTrackFingerprint(title) {
            if (currentFingerprintTrackNum) {
                const trackItem = document.querySelector('.track-item[data-track-num="' + currentFingerprintTrackNum + '"]');
                const input = document.querySelector('input[name="track_' + currentFingerprintTrackNum + '"]');
                
                if (input) {
                    input.value = title;
                    // Add fingerprinted styling to the input
                    input.classList.add('fingerprinted');
                }
                
                // Show the waveform indicator
                if (trackItem) {
                    const indicator = trackItem.querySelector('.track-fingerprinted');
                    if (indicator) {
                        indicator.classList.add('show');
                    }
                }
            }
            closeFingerprintResults();
        }
        
        function closeFingerprintResults() {
            document.getElementById('trackFingerprintResults').style.display = 'none';
            currentFingerprintTrackNum = null;
        }
        
        function escapeAttr(str) {
            if (!str) return '';
            return String(str).replace(/"/g, '&quot;').replace(/'/g, '&#39;').replace(/\\/g, '\\\\');
        }
        
        async function fetchMetadata() {
            const artist = document.getElementById('fetchArtist').value.trim();
            const album = document.getElementById('fetchAlbum').value.trim();
            const status = document.getElementById('fetchStatus');
            const btn = document.getElementById('fetchBtn');
            let resultsDiv = document.getElementById('fetchResults');
            
            if (!resultsDiv) {
                resultsDiv = document.createElement('div');
                resultsDiv.id = 'fetchResults';
                status.parentNode.insertBefore(resultsDiv, status.nextSibling);
            }
            
            if (!artist && !album) {
                status.className = 'identify-status error';
                status.textContent = 'Please enter artist and/or album name';
                status.style.display = 'block';
                resultsDiv.style.display = 'none';
                return;
            }
            
            btn.disabled = true;
            btn.innerHTML = '<span class="spinner"></span>Searching...';
            status.className = 'identify-status loading';
            status.textContent = 'Searching MusicBrainz...';
            status.style.display = 'block';
            resultsDiv.innerHTML = '';
            resultsDiv.style.display = 'none';
            
            try {
                const trackInputs = document.querySelectorAll('input[name^="track_"]');
                const trackCount = trackInputs.length;
                const response = await fetch('/api/search-releases?artist=' + encodeURIComponent(artist) + '&album=' + encodeURIComponent(album) + '&track_count=' + trackCount);
                const data = await response.json();
                
                if (data.success && data.results && data.results.length > 0) {
                    status.className = 'identify-status success';
                    status.textContent = 'Found ' + data.results.length + ' result(s). Click to fill form:';
                    let html = '<div style="border:1px solid rgba(255,255,255,0.2);border-radius:8px;margin-top:12px;overflow:hidden;">';
                    data.results.forEach(function(r) {
                        const matchBadge = r.track_match ? '<span style="background:#4ecca3;color:#1a1a2e;padding:2px 6px;border-radius:4px;font-size:0.75rem;margin-left:8px;">TRACKS MATCH</span>' : '';
                        html += '<div class="fetch-result" data-id="' + r.id + '" style="padding:12px;border-bottom:1px solid rgba(255,255,255,0.1);cursor:pointer;">';
                        html += '<div style="font-weight:500;">' + escapeHtml(r.artist) + ' - ' + escapeHtml(r.album) + '</div>';
                        html += '<div style="font-size:0.85rem;color:#888;">' + (r.year || 'Unknown year') + ' | ' + r.track_count + ' tracks' + matchBadge + '</div>';
                        html += '</div>';
                    });
                    html += '</div>';
                    resultsDiv.innerHTML = html;
                    resultsDiv.style.display = 'block';
                    
                    // Add click handlers using event delegation
                    resultsDiv.querySelectorAll('.fetch-result').forEach(function(el) {
                        el.onclick = function() { selectRelease(this.getAttribute('data-id')); };
                        el.onmouseover = function() { this.style.background = 'rgba(255,255,255,0.1)'; };
                        el.onmouseout = function() { this.style.background = 'transparent'; };
                    });
                } else {
                    status.className = 'identify-status error';
                    status.textContent = 'No results found. Try different search terms.';
                    resultsDiv.style.display = 'none';
                }
            } catch (e) {
                status.className = 'identify-status error';
                status.textContent = 'Error connecting to server.';
                resultsDiv.style.display = 'none';
            }
            
            btn.disabled = false;
            btn.textContent = 'Search MusicBrainz';
        }
        
        function escapeHtml(text) {
            if (!text) return '';
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }
        
        async function selectRelease(releaseId) {
            const status = document.getElementById('fetchStatus');
            const resultsDiv = document.getElementById('fetchResults');
            status.className = 'identify-status loading';
            status.textContent = 'Loading album details...';
            
            try {
                const response = await fetch('/api/fetch-release?id=' + encodeURIComponent(releaseId));
                const data = await response.json();
                
                if (data.success) {
                    document.getElementById('artist').value = data.artist || '';
                    document.getElementById('album_name').value = data.album || '';
                    document.getElementById('year').value = data.year || '';
                    
                    if (data.tracks && data.tracks.length > 0) {
                        data.tracks.forEach(function(title, i) {
                            const input = document.querySelector('input[name="track_' + (i + 1) + '"]');
                            if (input && title) input.value = title;
                        });
                    }
                    
                    if (data.art_url) {
                        loadImageFromUrl(data.art_url);
                    }
                    
                    status.className = 'identify-status success';
                    status.textContent = 'Form filled! Review and save.';
                    if (resultsDiv) resultsDiv.style.display = 'none';
                } else {
                    status.className = 'identify-status error';
                    status.textContent = 'Failed to load album details.';
                }
            } catch (e) {
                status.className = 'identify-status error';
                status.textContent = 'Error loading details.';
            }
        }
        
        // Album art functions
        const dropZone = document.getElementById('dropZone');
        const artFile = document.getElementById('artFile');
        const artUrl = document.getElementById('artUrl');
        const artPreview = document.getElementById('artPreview');
        
        if (dropZone) {
            dropZone.addEventListener('click', (e) => {
                if (e.target.tagName !== 'INPUT') artFile.click();
            });
            dropZone.addEventListener('dragover', (e) => { e.preventDefault(); dropZone.classList.add('dragover'); });
            dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));
            dropZone.addEventListener('drop', (e) => {
                e.preventDefault();
                dropZone.classList.remove('dragover');
                if (e.dataTransfer.files.length) {
                    artFile.files = e.dataTransfer.files;
                    previewImage(e.dataTransfer.files[0]);
                }
            });
            artFile.addEventListener('change', (e) => { if (e.target.files.length) previewImage(e.target.files[0]); });
            
            // Handle paste events for URLs or images
            document.addEventListener('paste', (e) => {
                // Check if we're in an input field (don't intercept normal paste)
                if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
                
                const items = e.clipboardData.items;
                for (let item of items) {
                    // Handle pasted image files
                    if (item.type.startsWith('image/')) {
                        e.preventDefault();
                        const file = item.getAsFile();
                        previewImage(file);
                        // Create a DataTransfer to set the file input
                        const dt = new DataTransfer();
                        dt.items.add(file);
                        artFile.files = dt.files;
                        return;
                    }
                    // Handle pasted text (URL)
                    if (item.type === 'text/plain') {
                        item.getAsString((text) => {
                            text = text.trim();
                            if (text.match(/^https?:\/\/.+\.(jpg|jpeg|png|gif|webp)/i) || text.match(/^https?:\/\/.*coverartarchive/i)) {
                                loadImageFromUrl(text);
                            }
                        });
                    }
                }
            });
        }
        
        function previewImage(file) {
            const reader = new FileReader();
            reader.onload = (e) => {
                artPreview.innerHTML = '<img src="' + e.target.result + '">';
                artPreview.classList.add('has-image');
            };
            reader.readAsDataURL(file);
        }
        
        function loadImageFromUrl(url) {
            artPreview.innerHTML = '<span class="art-placeholder">Loading...</span>';
            const img = new Image();
            img.onload = () => {
                artPreview.innerHTML = '';
                artPreview.appendChild(img);
                artPreview.classList.add('has-image');
                artUrl.value = url;  // Store URL for form submission
            };
            img.onerror = () => {
                artPreview.innerHTML = '<span class="art-placeholder" style="color:#f66">Failed</span>';
            };
            img.src = url;
        }
        
        function showConfirmModal() { document.getElementById('confirmModal').classList.add('show'); }
        function hideConfirmModal() { document.getElementById('confirmModal').classList.remove('show'); }
        function submitForm() { document.getElementById('tagForm').submit(); }
        
        function confirmEdit(path, artist, album) {
            document.getElementById('browseConfirmText').textContent = 'You are about to edit: ' + artist + ' - ' + album;
            document.getElementById('browseConfirmLink').href = '/edit-existing?path=' + encodeURIComponent(path);
            document.getElementById('browseConfirmModal').classList.add('show');
        }
        function hideBrowseModal() { document.getElementById('browseConfirmModal').classList.remove('show'); }
        
        // Event delegation for album clicks (avoids inline JS escaping issues with special characters)
        document.addEventListener('click', function(e) {
            var item = e.target.closest('.album-clickable');
            if (item) {
                var path = item.getAttribute('data-path');
                var artist = item.getAttribute('data-artist');
                var album = item.getAttribute('data-album');
                if (path && artist && album) {
                    confirmEdit(path, artist, album);
                }
                return;
            }
            var unknownItem = e.target.closest('.album-unknown');
            if (unknownItem) {
                var albumName = unknownItem.getAttribute('data-album');
                if (albumName) {
                    location.href = '/edit/' + encodeURIComponent(albumName);
                }
            }
        });
        
        // Delete album functions
        function showDeleteModal() {
            document.getElementById('deletePath').value = {{ (album.original_path if album else "")|tojson }};
            document.getElementById('deleteModal').classList.add('show');
        }
        function hideDeleteModal() { document.getElementById('deleteModal').classList.remove('show'); }
        function confirmDelete() {
            document.getElementById('deleteKeepBackup').value = document.getElementById('keepBackup').checked ? 'true' : 'false';
            document.getElementById('deleteForm').submit();
        }
        
        // Audio player functions
        let currentPlayBtn = null;
        const audioPlayer = document.getElementById('audioPlayer');
        const nowPlaying = document.getElementById('nowPlaying');
        const nowPlayingText = document.getElementById('nowPlayingText');
        const audioSeek = document.getElementById('audioSeek');
        
        function playTrack(path, btn) {
            if (!audioPlayer) return;
            
            // If clicking same track, toggle play/pause
            if (currentPlayBtn === btn && !audioPlayer.paused) {
                audioPlayer.pause();
                btn.classList.remove('playing');
                btn.innerHTML = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>';
                return;
            }
            
            // Reset previous button
            if (currentPlayBtn && currentPlayBtn !== btn) {
                currentPlayBtn.classList.remove('playing');
                currentPlayBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>';
            }
            
            currentPlayBtn = btn;
            btn.classList.add('playing');
            btn.innerHTML = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>';
            
            // Get track name from input
            const trackItem = btn.closest('.track-item');
            const trackInput = trackItem.querySelector('.track-title');
            const trackName = trackInput.value || 'Track ' + trackItem.querySelector('.track-num').textContent;
            
            audioPlayer.src = '/api/audio?path=' + encodeURIComponent(path);
            audioPlayer.play();
            
            if (nowPlaying) {
                nowPlaying.style.display = 'flex';
                nowPlayingText.textContent = 'Playing: ' + trackName;
            }
        }
        
        function stopAudio() {
            if (!audioPlayer) return;
            audioPlayer.pause();
            audioPlayer.currentTime = 0;
            if (nowPlaying) nowPlaying.style.display = 'none';
            if (currentPlayBtn) {
                currentPlayBtn.classList.remove('playing');
                currentPlayBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>';
                currentPlayBtn = null;
            }
        }
        
        if (audioPlayer) {
            audioPlayer.addEventListener('ended', stopAudio);
            audioPlayer.addEventListener('timeupdate', function() {
                if (audioSeek && audioPlayer.duration) {
                    audioSeek.value = (audioPlayer.currentTime / audioPlayer.duration) * 100;
                }
            });
        }
        
        if (audioSeek) {
            audioSeek.addEventListener('input', function() {
                if (audioPlayer && audioPlayer.duration) {
                    audioPlayer.currentTime = (this.value / 100) * audioPlayer.duration;
                }
            });
        }
        
        // Load existing album art on page load
        (function() {
            const albumPath = {{ (album.original_path if album else "")|tojson }};
            const hasArt = {{ 'true' if album and album.has_art else 'false' }};
            
            if (albumPath && hasArt && artPreview) {
                artPreview.innerHTML = '<span class="art-placeholder">Loading...</span>';
                
                const img = new Image();
                img.onload = function() {
                    artPreview.innerHTML = '';
                    artPreview.appendChild(img);
                    artPreview.classList.add('has-image');
                };
                img.onerror = function() {
                    artPreview.innerHTML = '<span class="art-placeholder">No image</span>';
                };
                img.src = '/api/album-art?path=' + encodeURIComponent(albumPath);
            }
        })();
        
        // Lazy load album thumbnails in browse/search views
        (function() {
            const thumbs = document.querySelectorAll('.album-thumb[data-path]');
            thumbs.forEach(function(thumb) {
                const path = thumb.getAttribute('data-path');
                if (!path) return;
                
                const img = new Image();
                img.onload = function() {
                    thumb.innerHTML = '';
                    thumb.appendChild(img);
                };
                // On error, keep the default music icon
                img.src = '/api/album-art?path=' + encodeURIComponent(path);
            });
        })();
    </script>
</body>
</html>
'''

# =============================================================================
# ROUTES
# =============================================================================

@app.route('/api/fetch-metadata')
def api_fetch_metadata():
    """API endpoint to fetch metadata from MusicBrainz"""
    artist = request.args.get('artist', '').strip()
    album = request.args.get('album', '').strip()
    
    if not artist or not album:
        return jsonify({'success': False, 'error': 'Artist and album are required'})
    
    metadata = fetch_musicbrainz_metadata(artist, album)
    
    if metadata:
        return jsonify({
            'success': True,
            'artist': metadata['artist'],
            'album': metadata['album'],
            'year': metadata['year'],
            'genre': metadata['genre'],
            'tracks': metadata['tracks'],
            'art_url': metadata['art_url']
        })
    else:
        return jsonify({'success': False, 'error': 'Album not found on MusicBrainz'})



@app.route('/api/search-releases')
def api_search_releases():
    """Search MusicBrainz and return multiple candidates"""
    artist = request.args.get('artist', '').strip()
    album = request.args.get('album', '').strip()
    track_count = int(request.args.get('track_count', 0))
    if not artist and not album:
        return jsonify({'success': False, 'error': 'Artist or album required'})
    results = search_musicbrainz_releases(artist, album, track_count)
    return jsonify({'success': True, 'results': results}) if results else jsonify({'success': False, 'results': []})

@app.route('/api/fetch-release')
def api_fetch_release():
    """Get full details for a selected release"""
    release_id = request.args.get('id', '').strip()
    if not release_id:
        return jsonify({'success': False, 'error': 'Release ID required'})
    meta = fetch_release_details(release_id)
    return jsonify({'success': True, **meta}) if meta else jsonify({'success': False, 'error': 'Failed'})


@app.route('/api/fingerprint-track')
def api_fingerprint_track():
    """Fingerprint a single track and return possible matches for user to choose"""
    import sys
    
    def log(msg):
        print(f"[FINGERPRINT] {msg}", file=sys.stderr, flush=True)
    
    file_path = request.args.get('path', '').strip()
    
    if not file_path or not os.path.exists(file_path):
        return jsonify({'success': False, 'error': 'File path required'})
    
    if not file_path.endswith('.flac'):
        return jsonify({'success': False, 'error': 'Must be a FLAC file'})
    
    log(f"Fingerprinting single track: {file_path}")
    
    try:
        duration, fingerprint = acoustid.fingerprint_file(file_path)
        log(f"  Fingerprint generated, duration: {duration:.1f}s")
        
        results = acoustid.lookup(ACOUSTID_API_KEY, fingerprint, duration, meta='recordings releases')
        
        if not results.get('results'):
            log(f"  NO RESULTS from AcoustID")
            return jsonify({'success': False, 'error': 'No matches found in AcoustID database'})
        
        log(f"  Got {len(results['results'])} AcoustID results")
        
        # Collect unique track titles with their best scores
        seen_titles = {}  # title.lower -> {title, artist, album, year, score}
        
        for result in results['results']:
            score = result.get('score', 0)
            if score < 0.5:
                continue
            
            for recording in result.get('recordings', []):
                title = recording.get('title')
                artists = recording.get('artists', [])
                artist = artists[0].get('name') if artists else None
                
                if not title or not artist:
                    continue
                
                # Get album info from first release
                album = ''
                year = ''
                for release in recording.get('releases', []):
                    if release.get('title'):
                        album = release.get('title')
                        date = release.get('date', '')
                        year = date[:4] if date and len(date) >= 4 else ''
                        break
                
                # Keep the best score for each unique title
                title_key = title.lower()
                if title_key not in seen_titles or score > seen_titles[title_key]['score']:
                    seen_titles[title_key] = {
                        'title': title,
                        'artist': artist,
                        'album': album,
                        'year': year,
                        'score': score
                    }
        
        if not seen_titles:
            return jsonify({'success': False, 'error': 'No valid matches found'})
        
        # Convert to list and sort by score
        matches = list(seen_titles.values())
        matches.sort(key=lambda x: -x['score'])
        
        # Limit to top 10 unique titles
        matches = matches[:10]
        
        log(f"  Returning {len(matches)} unique title matches")
        for m in matches[:5]:
            log(f"    {m['score']:.0%}: '{m['title']}' by {m['artist']}")
        
        return jsonify({
            'success': True,
            'matches': [{
                'title': m['title'],
                'artist': m['artist'],
                'album': m['album'],
                'year': m['year'],
                'score': round(m['score'] * 100)
            } for m in matches]
        })
        
    except Exception as e:
        log(f"  ERROR: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)})
    
    return jsonify({
        'success': True,
        'identified_count': identified_count,
        'total_tracks': total_tracks,
        'candidates': candidates[:5],  # Top 5 candidates
        'track_details': track_results
    })


@app.route('/api/album-art')
def api_album_art():
    """Extract and serve embedded album art from FLAC files"""
    path = request.args.get('path', '')
    if not path or not os.path.exists(path):
        return Response(status=404)
    
    # Find first FLAC with embedded art
    flac_files = list(Path(path).glob("*.flac"))
    for flac_path in flac_files:
        try:
            audio = FLAC(str(flac_path))
            if audio.pictures:
                pic = audio.pictures[0]
                return Response(pic.data, mimetype=pic.mime)
        except:
            pass
    return Response(status=404)


@app.route('/api/audio')
def api_audio():
    """Serve audio file for preview playback"""
    path = request.args.get('path', '')
    if not path:
        return Response(status=400)
    
    # URL decode the path
    path = urllib.parse.unquote(path)
    
    # Security: ensure path is within MUSIC_DIR
    real_path = os.path.realpath(path)
    if not real_path.startswith(os.path.realpath(MUSIC_DIR)):
        return Response(status=403)
    
    if not os.path.exists(real_path) or not real_path.endswith('.flac'):
        return Response(status=404)
    
    return send_file(real_path, mimetype='audio/flac')


@app.route('/')
def index():
    albums = []
    can_undo = get_last_undoable() is not None
    counts = get_album_counts()
    
    if os.path.exists(UNKNOWN_DIR):
        for folder in os.listdir(UNKNOWN_DIR):
            folder_path = os.path.join(UNKNOWN_DIR, folder)
            if os.path.isdir(folder_path):
                flac_files = list(Path(folder_path).glob("*.flac"))
                if flac_files:
                    albums.append({'folder': folder, 'name': folder.replace('_', ' '), 'track_count': len(flac_files)})
    
    return render_template_string(HTML_TEMPLATE, albums=albums, can_undo=can_undo, counts=counts)


@app.route('/search')
def search():
    query = request.args.get('q', '').strip()
    can_undo = get_last_undoable() is not None
    counts = get_album_counts()
    if not query:
        return redirect('/')
    results = search_library(query)
    return render_template_string(HTML_TEMPLATE, search_results=results, search_query=query, view='search', can_undo=can_undo, counts=counts)


@app.route('/browse')
def browse():
    albums = []
    can_undo = get_last_undoable() is not None
    counts = get_album_counts()
    if os.path.exists(MUSIC_DIR):
        for item in sorted(os.listdir(MUSIC_DIR)):
            item_path = os.path.join(MUSIC_DIR, item)
            if os.path.isdir(item_path) and item != "Unknown Artist":
                # Check if this directory contains FLAC files directly (flat album structure)
                flac_files = list(Path(item_path).glob("*.flac"))
                if flac_files:
                    # This is a flat album (no artist subfolder) - treat folder name as album
                    albums.append({'artist': 'Various Artists', 'album_name': item, 'path': item_path, 'track_count': len(flac_files)})
                else:
                    # This is an artist folder - look for album subfolders
                    for album_name in sorted(os.listdir(item_path)):
                        album_path = os.path.join(item_path, album_name)
                        if os.path.isdir(album_path):
                            album_flacs = list(Path(album_path).glob("*.flac"))
                            if album_flacs:
                                albums.append({'artist': item, 'album_name': album_name, 'path': album_path, 'track_count': len(album_flacs)})
    return render_template_string(HTML_TEMPLATE, albums=albums, view='browse', can_undo=can_undo, counts=counts)


@app.route('/history')
def history():
    hist = load_history()
    can_undo = get_last_undoable() is not None
    counts = get_album_counts()
    return render_template_string(HTML_TEMPLATE, history=hist, view='history', can_undo=can_undo, counts=counts)


@app.route('/edit/<folder>')
def edit(folder):
    folder_path = os.path.join(UNKNOWN_DIR, folder)
    can_undo = get_last_undoable() is not None
    if not os.path.exists(folder_path):
        return render_template_string(HTML_TEMPLATE, message="Album not found", message_type="error", can_undo=can_undo)
    album = get_album_info(folder_path, folder)
    return render_template_string(HTML_TEMPLATE, album=album, is_existing=False, can_undo=can_undo)


@app.route('/edit-existing')
def edit_existing():
    path = request.args.get('path')
    can_undo = get_last_undoable() is not None
    if not path or not os.path.exists(path):
        return render_template_string(HTML_TEMPLATE, message="Album not found", message_type="error", can_undo=can_undo)
    folder = os.path.basename(path)
    album = get_album_info(path, folder)
    album['original_path'] = path
    return render_template_string(HTML_TEMPLATE, album=album, is_existing=True, back_url='/browse', can_undo=can_undo)


def get_album_info(folder_path, folder):
    flac_files = sorted(Path(folder_path).glob("*.flac"))
    tracks = []
    album_artist = ""
    album_name = ""
    year = ""
    genre = ""
    disc_number = ""
    disc_total = ""
    has_art = False
    is_compilation = False
    
    for i, f in enumerate(flac_files, 1):
        try:
            audio = FLAC(str(f))
            title = audio.get('title', [f.stem])[0]
            track_artist = audio.get('artist', [''])[0]
            
            if i == 1:
                # Prefer ALBUMARTIST for the album-level artist
                album_artist = audio.get('albumartist', [''])[0] or track_artist
                album_name = audio.get('album', [''])[0]
                year = audio.get('date', [''])[0]
                genre = audio.get('genre', [''])[0]
                disc_number = audio.get('discnumber', [''])[0]
                disc_total = audio.get('disctotal', [''])[0]
                has_art = len(audio.pictures) > 0
                # Check if marked as compilation
                is_compilation = audio.get('compilation', [''])[0] == '1'
            
            # For compilations or when track artist differs from album artist,
            # format as "Artist - Title" to preserve the per-track artist info
            if track_artist and track_artist != album_artist:
                title = f"{track_artist} - {title}"
                is_compilation = True  # Mark as compilation if artists differ
                
        except:
            title = f.stem
            track_artist = ""
            
        if title.lower().startswith('track ') and title[6:].isdigit():
            title = ""
        tracks.append({'num': i, 'title': title, 'file': f.name, 'path': str(f)})
    
    # If we detected differing artists, use "Various Artists" as album artist
    if is_compilation and album_artist and album_artist.lower() not in ['various artists', 'various', 'va', 'soundtrack', 'ost']:
        album_artist = "Various Artists"
        
    if album_artist == "Unknown Artist": album_artist = ""
    if album_name == "Unknown Album": album_name = ""
    return {'folder': folder, 'original_path': folder_path, 'artist': album_artist, 'album': album_name, 'year': year, 'genre': genre, 'disc_number': disc_number, 'disc_total': disc_total, 'has_art': has_art, 'tracks': tracks}


@app.route('/edit/<folder>', methods=['POST'])
@app.route('/edit-existing', methods=['POST'])
def save(folder=None):
    original_path = request.form.get('original_path', '')
    is_existing = request.form.get('is_existing') == 'True'
    can_undo = get_last_undoable() is not None
    
    if is_existing and original_path:
        folder_path = original_path
    else:
        folder = request.form.get('folder', folder)
        folder_path = os.path.join(UNKNOWN_DIR, folder)
    
    if not os.path.exists(folder_path):
        return render_template_string(HTML_TEMPLATE, message="Album not found", message_type="error", can_undo=can_undo)
    
    artist = request.form.get('artist', '').strip()
    album_name = request.form.get('album_name', '').strip()
    year = request.form.get('year', '').strip()
    genre = request.form.get('genre', '').strip()
    disc_number = request.form.get('disc_number', '').strip()
    disc_total = request.form.get('disc_total', '').strip()
    
    if not artist or not album_name:
        return render_template_string(HTML_TEMPLATE, message="Artist and Album name are required", message_type="error", can_undo=can_undo)
    
    backup_path = create_backup(folder_path)
    
    art_data = None
    if 'art_file' in request.files:
        art_file = request.files['art_file']
        if art_file.filename:
            art_data = process_image(art_file.read())
    
    if not art_data and request.form.get('art_url'):
        art_url = request.form['art_url'].strip()
        if art_url:
            try:
                print(f"Fetching album art from URL: {art_url}")
                resp = requests.get(art_url, timeout=10, headers={'User-Agent': 'MusicTagger/1.0'})
                if resp.status_code == 200:
                    art_data = process_image(resp.content)
                    if art_data:
                        print(f"Successfully processed album art ({len(art_data)} bytes)")
                    else:
                        print(f"Failed to process image from URL")
                else:
                    print(f"Failed to fetch art URL: HTTP {resp.status_code}")
            except Exception as e:
                print(f"Error fetching album art from URL: {e}")
    
    flac_files = sorted(Path(folder_path).glob("*.flac"))
    safe_artist = sanitize(artist)
    safe_album = sanitize(album_name)
    new_dir = os.path.join(MUSIC_DIR, safe_artist, safe_album)
    same_location = os.path.normpath(new_dir) == os.path.normpath(folder_path)
    
    if not same_location and os.path.exists(new_dir):
        return render_template_string(HTML_TEMPLATE, message=f"Folder already exists: {safe_artist}/{safe_album}", message_type="error", can_undo=can_undo)
    
    if not same_location:
        os.makedirs(new_dir, exist_ok=True)
    
    # Check if this is a compilation album (Various Artists, Soundtrack, etc.)
    compilation_keywords = ['various artists', 'various', 'va', 'soundtrack', 'ost', 'compilation', 'sampler']
    is_compilation = artist.lower().strip() in compilation_keywords or any(kw in artist.lower() for kw in ['various', 'soundtrack', 'ost'])
    
    if is_compilation:
        print(f"[SAVE] Detected compilation album: {artist} - {album_name}")
    
    for i, flac_path in enumerate(flac_files, 1):
        title = request.form.get(f'track_{i}', '').strip()
        if not title:
            title = f"Track {i}"
        
        # For compilations, try to parse "Artist - Title" format
        track_artist = artist  # Default to album artist
        track_title = title
        
        if is_compilation and ' - ' in title:
            # Split on first " - " to get artist and title
            parts = title.split(' - ', 1)
            if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                track_artist = parts[0].strip()
                track_title = parts[1].strip()
                print(f"[SAVE] Track {i}: Parsed artist='{track_artist}', title='{track_title}'")
        
        try:
            audio = FLAC(str(flac_path))
            
            # Preserve existing album art if no new art provided
            existing_pictures = []
            if not art_data and audio.pictures:
                existing_pictures = list(audio.pictures)
            
            audio.delete()  # Removes metadata tags
            audio.clear_pictures()  # Removes embedded artwork
            audio['ARTIST'] = track_artist
            audio['ALBUMARTIST'] = artist  # Always set album artist
            audio['ALBUM'] = album_name
            audio['TITLE'] = track_title
            audio['TRACKNUMBER'] = str(i)
            audio['TRACKTOTAL'] = str(len(flac_files))
            if is_compilation:
                audio['COMPILATION'] = '1'  # Mark as compilation
            if year: audio['DATE'] = year
            if genre: audio['GENRE'] = genre
            if disc_number: audio['DISCNUMBER'] = disc_number
            if disc_total: audio['DISCTOTAL'] = disc_total
            
            # Add new art, or restore existing art
            if art_data:
                pic = Picture()
                pic.type = 3  # Front cover
                pic.mime = 'image/jpeg'
                pic.data = art_data
                audio.add_picture(pic)
            elif existing_pictures:
                for pic in existing_pictures:
                    audio.add_picture(pic)
            
            audio.save()
        except Exception as e:
            print(f"Error tagging {flac_path}: {e}")
        
        if not same_location:
            # For compilations, include artist in filename
            if is_compilation and track_artist != artist:
                safe_track_artist = sanitize(track_artist)
                safe_title = sanitize(track_title)
                new_name = f"{i:02d} - {safe_track_artist} - {safe_title}.flac"
            else:
                safe_title = sanitize(track_title)
                new_name = f"{i:02d} - {safe_title}.flac"
            new_path = os.path.join(new_dir, new_name)
            shutil.move(str(flac_path), new_path)
    
    if not same_location:
        try:
            # Remove entire source folder (including any leftover non-FLAC files)
            # This ensures clean state for undo operations
            if os.path.exists(folder_path):
                shutil.rmtree(folder_path)
            parent = os.path.dirname(folder_path)
            if os.path.exists(parent) and not os.listdir(parent):
                os.rmdir(parent)
        except Exception as e:
            print(f"Warning: Could not fully clean up {folder_path}: {e}")
    
    action = "Updated metadata" if same_location else f"Moved from {os.path.basename(folder_path)}"
    add_history_entry({'artist': artist, 'album': album_name, 'action': action, 'original_path': folder_path, 'new_path': new_dir, 'backup_path': backup_path})
    
    try:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            s.connect(('lms', 9090))
            s.send(b'rescan\n')
    except:
        pass
    
    return render_template_string(HTML_TEMPLATE, message=f"Success! Saved as: {artist} - {album_name}", message_type="success", albums=[], can_undo=True)


@app.route('/undo', methods=['POST'])
def undo():
    entry = get_last_undoable()
    if not entry:
        # No undoable action found
        albums = []
        if os.path.exists(UNKNOWN_DIR):
            for folder in os.listdir(UNKNOWN_DIR):
                folder_path = os.path.join(UNKNOWN_DIR, folder)
                if os.path.isdir(folder_path):
                    flac_files = list(Path(folder_path).glob("*.flac"))
                    if flac_files:
                        albums.append({'folder': folder, 'name': folder.replace('_', ' '), 'track_count': len(flac_files)})
        return render_template_string(HTML_TEMPLATE, 
            albums=albums, 
            message="Nothing to undo", 
            message_type="error",
            can_undo=False)
    
    backup_path = entry.get('backup_path')
    new_path = entry.get('new_path')
    original_path = entry.get('original_path')
    artist = entry.get('artist', 'Unknown')
    album = entry.get('album', 'Unknown')
    action = entry.get('action', '')
    
    if backup_path and os.path.exists(backup_path):
        try:
            # Remove the new location if it exists
            if new_path and os.path.exists(new_path):
                shutil.rmtree(new_path)
                parent = os.path.dirname(new_path)
                try:
                    if os.path.exists(parent) and not os.listdir(parent):
                        os.rmdir(parent)
                except:
                    pass
            
            # Remove original_path if it exists (leftover files from incomplete cleanup)
            # This prevents shutil.move from putting backup inside existing folder
            if os.path.exists(original_path):
                shutil.rmtree(original_path)
            
            os.makedirs(os.path.dirname(original_path), exist_ok=True)
            shutil.move(backup_path, original_path)
            
            # Mark as undone in history
            history = load_history()
            for h in history:
                if h.get('id') == entry.get('id'):
                    h['undone'] = True
                    break
            save_history(history)
            
            # Trigger LMS rescan
            try:
                import socket
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(2)
                    s.connect(('lms', 9090))
                    s.send(b'rescan\n')
            except:
                pass
            
            # Show success message
            albums = []
            if os.path.exists(UNKNOWN_DIR):
                for folder in os.listdir(UNKNOWN_DIR):
                    folder_path = os.path.join(UNKNOWN_DIR, folder)
                    if os.path.isdir(folder_path):
                        flac_files = list(Path(folder_path).glob("*.flac"))
                        if flac_files:
                            albums.append({'folder': folder, 'name': folder.replace('_', ' '), 'track_count': len(flac_files)})
            
            return render_template_string(HTML_TEMPLATE,
                albums=albums,
                message=f"Undo successful: Restored '{artist} - {album}'",
                message_type="success",
                can_undo=get_last_undoable() is not None)
        
        except Exception as e:
            # Handle undo failure
            albums = []
            if os.path.exists(UNKNOWN_DIR):
                for folder in os.listdir(UNKNOWN_DIR):
                    folder_path = os.path.join(UNKNOWN_DIR, folder)
                    if os.path.isdir(folder_path):
                        flac_files = list(Path(folder_path).glob("*.flac"))
                        if flac_files:
                            albums.append({'folder': folder, 'name': folder.replace('_', ' '), 'track_count': len(flac_files)})
            
            return render_template_string(HTML_TEMPLATE,
                albums=albums,
                message=f"Undo failed: {str(e)}",
                message_type="error",
                can_undo=True)
    else:
        # Backup doesn't exist
        albums = []
        if os.path.exists(UNKNOWN_DIR):
            for folder in os.listdir(UNKNOWN_DIR):
                folder_path = os.path.join(UNKNOWN_DIR, folder)
                if os.path.isdir(folder_path):
                    flac_files = list(Path(folder_path).glob("*.flac"))
                    if flac_files:
                        albums.append({'folder': folder, 'name': folder.replace('_', ' '), 'track_count': len(flac_files)})
        
        return render_template_string(HTML_TEMPLATE,
            albums=albums,
            message=f"Cannot undo: Backup for '{artist} - {album}' not found",
            message_type="error",
            can_undo=get_last_undoable() is not None)


@app.route('/delete', methods=['POST'])
def delete_album():
    """Delete an album with optional backup"""
    path = request.form.get('path', '')
    keep_backup = request.form.get('keep_backup') == 'true'
    can_undo = get_last_undoable() is not None
    
    if not path or not os.path.exists(path):
        return render_template_string(HTML_TEMPLATE, message="Album not found", message_type="error", can_undo=can_undo)
    
    # Security: ensure path is within MUSIC_DIR
    real_path = os.path.realpath(path)
    if not real_path.startswith(os.path.realpath(MUSIC_DIR)):
        return render_template_string(HTML_TEMPLATE, message="Invalid path", message_type="error", can_undo=can_undo)
    
    # Get album info for history
    folder_name = os.path.basename(path)
    parent_name = os.path.basename(os.path.dirname(path))
    
    backup_path = None
    if keep_backup:
        backup_path = create_backup(path)
    
    # Delete the album folder
    try:
        shutil.rmtree(path)
        # Clean up empty parent directory
        parent = os.path.dirname(path)
        if os.path.exists(parent) and not os.listdir(parent):
            os.rmdir(parent)
    except Exception as e:
        return render_template_string(HTML_TEMPLATE, message=f"Error deleting album: {e}", message_type="error", can_undo=can_undo)
    
    # Add to history
    add_history_entry({
        'artist': parent_name,
        'album': folder_name,
        'action': f"Deleted album{' (backup kept)' if keep_backup else ' (no backup)'}",
        'original_path': path,
        'new_path': None,
        'backup_path': backup_path
    })
    
    # Trigger LMS rescan
    try:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            s.connect(('lms', 9090))
            s.send(b'rescan\n')
    except:
        pass
    
    return render_template_string(HTML_TEMPLATE, 
        message=f"Deleted: {parent_name} - {folder_name}" + (" (backup saved)" if keep_backup else ""), 
        message_type="success", albums=[], can_undo=backup_path is not None)


def create_backup(folder_path):
    """Create a backup of the album folder. Only keeps one backup per album."""
    album_name = os.path.basename(folder_path)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    
    # Delete any existing backups for this same album (keep only 1 per album)
    for existing in Path(BACKUP_DIR).iterdir():
        if existing.is_dir() and existing.name.endswith(f"_{album_name}"):
            try:
                shutil.rmtree(existing)
                print(f"[BACKUP] Removed old backup: {existing.name}")
            except Exception as e:
                print(f"[BACKUP] Failed to remove old backup {existing.name}: {e}")
    
    # Create new backup with timestamp
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_name = f"backup_{timestamp}_{album_name}"
    backup_path = os.path.join(BACKUP_DIR, backup_name)
    shutil.copytree(folder_path, backup_path)
    print(f"[BACKUP] Created backup: {backup_name}")
    
    return backup_path


def sanitize(name):
    for char in ['/', '\\', ':', '*', '?', '"', '<', '>', '|']:
        name = name.replace(char, '-')
    return name.strip()


def process_image(data):
    try:
        img = Image.open(BytesIO(data))
        img = img.convert('RGB')
        if img.width > 800 or img.height > 800:
            img.thumbnail((800, 800), Image.LANCZOS)
        output = BytesIO()
        img.save(output, format='JPEG', quality=90)
        return output.getvalue()
    except:
        return None


def cleanup_stale_temp_files():
    """Remove stale abcde temp directories older than 24 hours and reset stuck jobs"""
    import time
    import sqlite3
    home_dir = "/home/arm"
    cutoff_time = time.time() - (24 * 60 * 60)  # 24 hours ago
    
    # 1. Clean up old temp directories
    try:
        for item in os.listdir(home_dir):
            if item.startswith("abcde."):
                item_path = os.path.join(home_dir, item)
                if os.path.isdir(item_path):
                    mtime = os.path.getmtime(item_path)
                    if mtime < cutoff_time:
                        shutil.rmtree(item_path)
                        print(f"[STARTUP CLEANUP] Removed stale temp directory: {item}")
    except Exception as e:
        print(f"[STARTUP CLEANUP] Error during temp file cleanup: {e}")
    
    # 2. Reset stuck database jobs older than 2 hours
    try:
        conn = sqlite3.connect('/home/arm/db/arm.db')
        cur = conn.cursor()
        # Find jobs that are stuck (not success/fail) and started more than 2 hours ago
        cur.execute("""
            SELECT job_id, title, status, start_time 
            FROM job 
            WHERE status NOT IN ('success', 'fail') 
            AND start_time < datetime('now', '-2 hours')
        """)
        stuck_jobs = cur.fetchall()
        
        if stuck_jobs:
            for job_id, title, status, start_time in stuck_jobs:
                cur.execute("UPDATE job SET status = 'fail', stop_time = datetime('now') WHERE job_id = ?", (job_id,))
                print(f"[STARTUP CLEANUP] Reset stuck job {job_id}: {title} ({status} -> fail, started {start_time})")
            conn.commit()
        conn.close()
    except Exception as e:
        print(f"[STARTUP CLEANUP] Error during database cleanup: {e}")


# =============================================================================
# ARM DIAGNOSTICS FUNCTIONS
# =============================================================================

def get_arm_diagnostics():
    """Get ARM system diagnostics for the debug view"""
    import subprocess
    import sqlite3
    import glob
    
    import time
    
    diagnostics = {
        'checks': [],
        'issues_found': 0,
        'rip_in_progress': False,
        'abcde_folders': [],
        'leftover_wavs': []
    }
    
    # Threshold: files/folders older than 2 hours are considered stale
    stale_threshold = time.time() - (2 * 60 * 60)
    
    # 1. Check for active rip processes FIRST (needed for subsequent checks)
    try:
        result = subprocess.run(['ps', 'aux'], capture_output=True, text=True, timeout=5)
        processes = result.stdout
        rip_procs = [line for line in processes.split('\n') if any(x in line for x in ['abcde', 'cdparanoia', 'flac']) and 'grep' not in line and 'ps aux' not in line]
        rip_procs = [p for p in rip_procs if p.strip()]
        if rip_procs:
            diagnostics['rip_in_progress'] = True
            diagnostics['checks'].append({'name': 'Active rip processes', 'status': 'info', 'message': 'Rip in progress'})
        else:
            diagnostics['checks'].append({'name': 'Active rip processes', 'status': 'ok', 'message': 'No rip processes running'})
    except:
        diagnostics['checks'].append({'name': 'Active rip processes', 'status': 'warning', 'message': 'Could not check processes'})
    
    # 2. Check for abcde temp folders
    abcde_folders = glob.glob("/home/arm/abcde.*")
    if abcde_folders:
        folder_info = []
        stale_folders = 0
        for folder in abcde_folders:
            try:
                mtime = os.path.getmtime(folder)
                is_stale = mtime < stale_threshold
                if is_stale:
                    stale_folders += 1
                size = sum(os.path.getsize(os.path.join(folder, f)) for f in os.listdir(folder) if os.path.isfile(os.path.join(folder, f)))
                size_mb = round(size / (1024 * 1024), 1)
                folder_info.append({'path': folder, 'name': os.path.basename(folder), 'size_mb': size_mb, 'stale': is_stale})
            except:
                folder_info.append({'path': folder, 'name': os.path.basename(folder), 'size_mb': 0, 'stale': False})
        diagnostics['abcde_folders'] = folder_info
        
        # Determine status based on rip state and staleness
        if diagnostics['rip_in_progress'] and stale_folders == 0:
            # Active rip with fresh temp folders - normal
            diagnostics['checks'].append({'name': 'Temp folders', 'status': 'info', 'message': f'{len(abcde_folders)} active temp folder(s)'})
        elif stale_folders > 0:
            # Stale folders exist - needs cleanup
            diagnostics['checks'].append({'name': 'Temp folders', 'status': 'warning', 'message': f'{stale_folders} stale temp folder(s) (>2 hrs old)'})
            diagnostics['issues_found'] += 1
        else:
            # Not ripping but fresh folders - unusual but not critical
            diagnostics['checks'].append({'name': 'Temp folders', 'status': 'info', 'message': f'{len(abcde_folders)} temp folder(s)'})
    else:
        diagnostics['checks'].append({'name': 'Temp folders', 'status': 'ok', 'message': 'No temp folders'})
    
    # 3. Check for leftover WAV files
    wav_files = glob.glob("/home/arm/**/*.wav", recursive=True)
    # Exclude WAV files in music directory (those might be intentional)
    wav_files = [f for f in wav_files if '/music/' not in f]
    if wav_files:
        diagnostics['leftover_wavs'] = wav_files[:10]  # Limit to 10
        # Check if any WAV files are stale
        stale_wavs = sum(1 for f in wav_files if os.path.exists(f) and os.path.getmtime(f) < stale_threshold)
        
        if diagnostics['rip_in_progress'] and stale_wavs == 0:
            # Active rip with fresh WAVs - normal
            diagnostics['checks'].append({'name': 'WAV files', 'status': 'info', 'message': f'{len(wav_files)} WAV file(s) (ripping)'})
        elif stale_wavs > 0:
            # Stale WAVs - needs cleanup
            diagnostics['checks'].append({'name': 'WAV files', 'status': 'warning', 'message': f'{stale_wavs} stale WAV file(s) (>2 hrs old)'})
            diagnostics['issues_found'] += 1
        else:
            diagnostics['checks'].append({'name': 'WAV files', 'status': 'info', 'message': f'{len(wav_files)} WAV file(s)'})
    else:
        diagnostics['checks'].append({'name': 'WAV files', 'status': 'ok', 'message': 'No leftover WAV files'})
    
    # 4. Check database for active/stuck jobs
    # Active = running for < 2 hours (normal), Stuck = running for > 2 hours (problematic)
    try:
        conn = sqlite3.connect('/home/arm/db/arm.db')
        cur = conn.cursor()
        # Get all incomplete jobs with their start time
        cur.execute("""
            SELECT job_id, title, status, start_time,
                   CASE WHEN start_time < datetime('now', '-2 hours') THEN 1 ELSE 0 END as is_stuck
            FROM job 
            WHERE status NOT IN ('success', 'fail')
        """)
        incomplete_jobs = cur.fetchall()
        conn.close()
        
        active_jobs = [j for j in incomplete_jobs if j[4] == 0]  # is_stuck = 0
        stuck_jobs = [j for j in incomplete_jobs if j[4] == 1]   # is_stuck = 1
        
        # Report active jobs (normal ripping)
        if active_jobs:
            diagnostics['checks'].append({'name': 'Active jobs', 'status': 'info', 'message': f'{len(active_jobs)} job(s) in progress'})
        else:
            diagnostics['checks'].append({'name': 'Active jobs', 'status': 'ok', 'message': 'No active jobs'})
        
        # Report stuck jobs (> 2 hours old, needs attention)
        if stuck_jobs:
            diagnostics['checks'].append({'name': 'Stuck jobs (>2 hrs)', 'status': 'warning', 'message': f'{len(stuck_jobs)} stuck job(s) - consider Reset ARM'})
            diagnostics['issues_found'] += 1
        else:
            diagnostics['checks'].append({'name': 'Stuck jobs (>2 hrs)', 'status': 'ok', 'message': 'No stuck jobs'})
    except Exception as e:
        diagnostics['checks'].append({'name': 'Database jobs', 'status': 'warning', 'message': f'Could not read database: {e}'})
    
    # 5. Check CD drive status
    try:
        if os.path.exists('/dev/sr0'):
            diagnostics['checks'].append({'name': 'CD drive', 'status': 'ok', 'message': 'Drive available at /dev/sr0'})
        else:
            diagnostics['checks'].append({'name': 'CD drive', 'status': 'error', 'message': 'CD drive not found'})
            diagnostics['issues_found'] += 1
    except:
        diagnostics['checks'].append({'name': 'CD drive', 'status': 'warning', 'message': 'Could not check drive'})
    
    # 6. Check raw/transcode directories
    raw_files = len(glob.glob("/home/arm/media/raw/*")) if os.path.exists("/home/arm/media/raw") else 0
    transcode_files = len(glob.glob("/home/arm/media/transcode/*")) if os.path.exists("/home/arm/media/transcode") else 0
    if raw_files > 0 or transcode_files > 0:
        diagnostics['checks'].append({'name': 'Media directories', 'status': 'info', 'message': f'Raw: {raw_files}, Transcode: {transcode_files} files'})
    else:
        diagnostics['checks'].append({'name': 'Media directories', 'status': 'ok', 'message': 'Media directories empty'})
    
    return diagnostics


def perform_arm_cleanup():
    """Clean up stale ARM temp files and reset stuck database jobs"""
    import glob
    import sqlite3
    
    results = {'deleted': [], 'errors': [], 'jobs_reset': []}
    
    # 1. Delete abcde temp folders
    for folder in glob.glob("/home/arm/abcde.*"):
        try:
            shutil.rmtree(folder)
            results['deleted'].append(folder)
        except Exception as e:
            results['errors'].append(f"{folder}: {e}")
    
    # 2. Delete leftover WAV files (not in music directory)
    for wav in glob.glob("/home/arm/**/*.wav", recursive=True):
        if '/music/' not in wav:
            try:
                os.remove(wav)
                results['deleted'].append(wav)
            except Exception as e:
                results['errors'].append(f"{wav}: {e}")
    
    # 3. Reset stuck database jobs (jobs not in 'success' or 'fail' state)
    try:
        conn = sqlite3.connect('/home/arm/db/arm.db')
        cur = conn.cursor()
        # Find stuck jobs
        cur.execute("SELECT job_id, title, status FROM job WHERE status NOT IN ('success', 'fail')")
        stuck_jobs = cur.fetchall()
        
        if stuck_jobs:
            for job_id, title, status in stuck_jobs:
                try:
                    cur.execute("UPDATE job SET status = 'fail', stop_time = datetime('now') WHERE job_id = ?", (job_id,))
                    results['jobs_reset'].append(f"Job {job_id}: {title} ({status} -> fail)")
                except Exception as e:
                    results['errors'].append(f"Job {job_id}: {e}")
            conn.commit()
        conn.close()
    except Exception as e:
        results['errors'].append(f"Database: {e}")
    
    return results


def get_disk_usage():
    """Get disk usage breakdown"""
    import subprocess
    
    usage = {
        'music_size': 0,
        'music_size_human': '0 B',
        'system_size': 0,
        'system_size_human': '0 B',
        'free_space': 0,
        'free_space_human': '0 B',
        'total_space': 0,
        'total_space_human': '0 B',
        'usage_percent': 0
    }
    
    def human_size(bytes_val):
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if bytes_val < 1024:
                return f"{bytes_val:.1f} {unit}"
            bytes_val /= 1024
        return f"{bytes_val:.1f} PB"
    
    try:
        # Get music library size
        result = subprocess.run(['du', '-sb', '/home/arm/music'], capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            usage['music_size'] = int(result.stdout.split()[0])
            usage['music_size_human'] = human_size(usage['music_size'])
    except:
        pass
    
    try:
        # Get system files size (ARM home minus music)
        result = subprocess.run(['du', '-sb', '/home/arm'], capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            total_arm = int(result.stdout.split()[0])
            usage['system_size'] = max(0, total_arm - usage['music_size'])
            usage['system_size_human'] = human_size(usage['system_size'])
    except:
        pass
    
    try:
        # Get disk free space using df
        result = subprocess.run(['df', '-B1', '/home/arm'], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            lines = result.stdout.strip().split('\n')
            if len(lines) >= 2:
                parts = lines[1].split()
                if len(parts) >= 4:
                    usage['total_space'] = int(parts[1])
                    usage['total_space_human'] = human_size(usage['total_space'])
                    usage['free_space'] = int(parts[3])
                    usage['free_space_human'] = human_size(usage['free_space'])
                    used = int(parts[2])
                    if usage['total_space'] > 0:
                        usage['usage_percent'] = round((used / usage['total_space']) * 100, 1)
    except:
        pass
    
    return usage


def get_recent_logs():
    """Get list of recent ARM log files"""
    import glob
    
    logs = []
    log_dir = "/home/arm/logs"
    
    if os.path.exists(log_dir):
        # Get all log files, sorted by modification time (newest first)
        log_files = glob.glob(os.path.join(log_dir, "*.log"))
        log_files = sorted(log_files, key=os.path.getmtime, reverse=True)[:10]
        
        for log_file in log_files:
            try:
                stat = os.stat(log_file)
                logs.append({
                    'name': os.path.basename(log_file),
                    'path': log_file,
                    'size': stat.st_size,
                    'modified': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M')
                })
            except:
                pass
    
    return logs


def read_log_file(log_path, lines=500):
    """Read the last N lines of a log file"""
    try:
        # Security check - must be in logs directory
        real_path = os.path.realpath(log_path)
        if not real_path.startswith('/home/arm/logs'):
            return None
        
        if not os.path.exists(log_path):
            return None
        
        with open(log_path, 'r', errors='replace') as f:
            all_lines = f.readlines()
            return ''.join(all_lines[-lines:])
    except:
        return None


@app.route('/debug')
def debug():
    """ARM Diagnostics page"""
    can_undo = get_last_undoable() is not None
    counts = get_album_counts()
    diagnostics = get_arm_diagnostics()
    disk_usage = get_disk_usage()
    recent_logs = get_recent_logs()
    return render_template_string(HTML_TEMPLATE, view='debug', diagnostics=diagnostics, 
                                  disk_usage=disk_usage, recent_logs=recent_logs,
                                  can_undo=can_undo, counts=counts)


@app.route('/api/eject-cd', methods=['POST'])
def api_eject_cd():
    """Eject the CD drive"""
    import subprocess
    try:
        result = subprocess.run(['eject', '/dev/sr0'], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return jsonify({'success': True, 'message': 'CD ejected successfully'})
        else:
            return jsonify({'success': False, 'error': result.stderr or 'Eject failed'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/logs/<path:log_name>')
def api_get_log(log_name):
    """Get contents of a specific log file"""
    log_path = os.path.join('/home/arm/logs', log_name)
    content = read_log_file(log_path, lines=500)
    if content is None:
        return jsonify({'success': False, 'error': 'Log file not found'})
    return jsonify({'success': True, 'content': content, 'name': log_name})


@app.route('/api/debug-clean', methods=['POST'])
def api_debug_clean():
    """API endpoint to clean up stale ARM files and reset stuck jobs"""
    # First check if a rip is in progress
    diagnostics = get_arm_diagnostics()
    if diagnostics['rip_in_progress']:
        return jsonify({'success': False, 'error': 'A rip appears to be in progress. Cannot reset now.'})
    
    results = perform_arm_cleanup()
    
    # Build message
    parts = []
    if results['deleted']:
        parts.append(f"Deleted {len(results['deleted'])} file(s)")
    if results['jobs_reset']:
        parts.append(f"Reset {len(results['jobs_reset'])} stuck job(s)")
    if results['errors']:
        parts.append(f"{len(results['errors'])} error(s)")
    
    message = ", ".join(parts) if parts else "Nothing to clean up"
    
    return jsonify({
        'success': True,
        'deleted': results['deleted'],
        'jobs_reset': results['jobs_reset'],
        'errors': results['errors'],
        'message': message
    })


# Run cleanup on startup
cleanup_stale_temp_files()


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
