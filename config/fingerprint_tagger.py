#!/usr/bin/env python3
"""
AcoustID Auto-Tagger for ARM Unknown Albums (Safe Mode)
Fixed: Searches through all recordings, not just the first one.
"""

import os
import sys
import shutil
from pathlib import Path
from collections import Counter
import acoustid
import musicbrainzngs
from mutagen.flac import FLAC

# Configuration
# API key loaded from environment variable (set in .env file or docker-compose)
ACOUSTID_API_KEY = os.environ.get("ACOUSTID_API_KEY", "")
MUSIC_DIR = "/home/arm/music"
UNKNOWN_ARTIST_DIR = os.path.join(MUSIC_DIR, "Unknown Artist")
MIN_CONFIDENCE = 0.80
MIN_IDENTIFIED_RATIO = 0.70
MIN_CONSENSUS_RATIO = 0.70

musicbrainzngs.set_useragent("ARM-AcoustID-Tagger", "1.0", "arm@localhost")


def log(msg, indent=0):
    prefix = "  " * indent
    print(f"{prefix}{msg}")


def fingerprint_and_lookup(filepath):
    """Fingerprint a file and look it up on AcoustID - searches ALL recordings"""
    try:
        duration, fingerprint = acoustid.fingerprint_file(filepath)
        results = acoustid.lookup(ACOUSTID_API_KEY, fingerprint, duration, 
                                   meta='recordings releases')
        
        if not results.get('results'):
            return None
        
        # Search through ALL results and recordings for valid metadata
        for result in results['results']:
            score = result.get('score', 0)
            if score < MIN_CONFIDENCE:
                continue
                
            if 'recordings' not in result:
                continue
            
            # Look through all recordings for one with valid metadata
            for recording in result['recordings']:
                title = recording.get('title')
                artists = recording.get('artists', [])
                artist = artists[0].get('name') if artists else None
                
                # Skip if no title or artist
                if not title or not artist:
                    continue
                
                # Found a valid recording!
                recording_id = recording.get('id')
                
                # Get album info from the recording's releases
                album = None
                year = None
                if 'releases' in recording and recording['releases']:
                    release = recording['releases'][0]
                    album = release.get('title')
                    date = release.get('date', '')
                    year = date[:4] if date and len(date) >= 4 else ''
                
                return {
                    'score': score,
                    'recording_id': recording_id,
                    'title': title,
                    'artist': artist,
                    'album': album or 'Unknown Album',
                    'year': year or ''
                }
        
        return None
        
    except Exception as e:
        log(f"Error looking up {filepath}: {e}", 2)
        return None


def sanitize_filename(name):
    if name is None:
        return "Unknown"
    for char in ['/', '\\', ':', '*', '?', '"', '<', '>', '|']:
        name = name.replace(char, '-')
    return name.strip()


def analyze_album(album_path):
    """Analyze an Unknown Album folder and return proposed changes"""
    log(f"Analyzing: {os.path.basename(album_path)}")
    
    flac_files = sorted(Path(album_path).glob("*.flac"))
    if not flac_files:
        return None, "No FLAC files found"
    
    log(f"Found {len(flac_files)} tracks", 1)
    
    track_info = []
    artists = []
    albums = []
    
    for i, flac_path in enumerate(flac_files, 1):
        log(f"[{i}/{len(flac_files)}] {flac_path.name}", 1)
        
        result = fingerprint_and_lookup(str(flac_path))
        if result:
            log(f"-> {result['title']} by {result['artist']} ({result['score']:.0%})", 2)
            
            artists.append(result['artist'])
            if result.get('album'):
                albums.append(result['album'])
            
            track_info.append({
                'path': flac_path,
                'track_num': i,
                'identified': True,
                **result
            })
        else:
            log(f"-> No match found", 2)
            original_title = flac_path.stem
            if original_title.startswith(f"{i:02d} - "):
                original_title = original_title[5:]
            track_info.append({
                'path': flac_path,
                'track_num': i,
                'identified': False,
                'title': original_title,
                'artist': None
            })
    
    # Check identification ratio
    identified_count = len([t for t in track_info if t.get('identified')])
    id_ratio = identified_count / len(flac_files)
    
    if id_ratio < MIN_IDENTIFIED_RATIO:
        return None, f"Only {identified_count}/{len(flac_files)} tracks identified ({id_ratio:.0%} < {MIN_IDENTIFIED_RATIO:.0%} required)"
    
    if not artists:
        return None, "No artists identified"
    
    # Check consensus
    artist_counts = Counter(artists)
    primary_artist, artist_count = artist_counts.most_common(1)[0]
    consensus_ratio = artist_count / len(artists)
    
    if consensus_ratio < MIN_CONSENSUS_RATIO:
        return None, f"No consensus on artist ({consensus_ratio:.0%} < {MIN_CONSENSUS_RATIO:.0%} required)"
    
    album_counts = Counter(albums) if albums else Counter()
    primary_album = album_counts.most_common(1)[0][0] if album_counts else "Unknown Album"
    
    # Get year
    year = ""
    for t in track_info:
        if t.get('year'):
            year = t['year']
            break
    
    return {
        'source_path': album_path,
        'artist': primary_artist,
        'album': primary_album,
        'year': year,
        'tracks': track_info,
        'identified_count': identified_count,
        'total_tracks': len(flac_files),
        'consensus_ratio': consensus_ratio
    }, None


def preview_changes(analysis):
    """Display what changes would be made"""
    print("\n" + "="*60)
    print("PROPOSED CHANGES")
    print("="*60)
    print(f"Artist: {analysis['artist']}")
    print(f"Album:  {analysis['album']}")
    if analysis['year']:
        print(f"Year:   {analysis['year']}")
    print(f"Tracks: {analysis['identified_count']}/{analysis['total_tracks']} identified")
    print(f"Consensus: {analysis['consensus_ratio']:.0%}")
    
    safe_artist = sanitize_filename(analysis['artist'])
    safe_album = sanitize_filename(analysis['album'])
    new_path = os.path.join(MUSIC_DIR, safe_artist, safe_album)
    
    print(f"\nDestination: {new_path}")
    print("\nTrack mapping:")
    for t in analysis['tracks']:
        old_name = t['path'].name
        title = t.get('title') or f"Track {t['track_num']}"
        new_name = f"{t['track_num']:02d} - {sanitize_filename(title)}.flac"
        
        if t.get('identified'):
            status = f"-> {new_name}"
        else:
            status = f"-> {new_name} (unidentified, keeping original name)"
        print(f"  {old_name} {status}")
    
    print("="*60)
    return new_path


def apply_changes(analysis, dry_run=True):
    """Apply the proposed changes"""
    if dry_run:
        log("\n[DRY RUN] No changes made. Use --apply to make changes.")
        return False
    
    safe_artist = sanitize_filename(analysis['artist'])
    safe_album = sanitize_filename(analysis['album'])
    new_album_path = os.path.join(MUSIC_DIR, safe_artist, safe_album)
    
    if os.path.exists(new_album_path):
        log(f"\nError: Destination already exists: {new_album_path}")
        log("Skipping to avoid overwriting existing files.")
        return False
    
    os.makedirs(new_album_path, exist_ok=True)
    log(f"\nCreated: {new_album_path}")
    
    for track in analysis['tracks']:
        old_path = track['path']
        title = track.get('title') or f"Track {track['track_num']}"
        
        safe_title = sanitize_filename(title)
        new_filename = f"{track['track_num']:02d} - {safe_title}.flac"
        new_path = os.path.join(new_album_path, new_filename)
        
        try:
            audio = FLAC(str(old_path))
            audio['ARTIST'] = analysis['artist']
            audio['ALBUM'] = analysis['album']
            audio['TITLE'] = title
            audio['TRACKNUMBER'] = str(track['track_num'])
            if analysis['year']:
                audio['DATE'] = analysis['year']
            audio.save()
            log(f"  Tagged: {new_filename}")
        except Exception as e:
            log(f"  Error tagging {old_path.name}: {e}")
        
        try:
            shutil.move(str(old_path), new_path)
        except Exception as e:
            log(f"  Error moving {old_path.name}: {e}")
    
    try:
        os.rmdir(analysis['source_path'])
        log(f"Removed empty folder: {os.path.basename(analysis['source_path'])}")
    except OSError:
        pass
    
    try:
        if os.path.exists(UNKNOWN_ARTIST_DIR) and not os.listdir(UNKNOWN_ARTIST_DIR):
            os.rmdir(UNKNOWN_ARTIST_DIR)
    except OSError:
        pass
    
    log(f"\nSuccessfully tagged and moved: {analysis['artist']} - {analysis['album']}")
    return True


def main():
    print("""
============================================================
  AcoustID Auto-Tagger for Unknown Albums (Safe Mode)
============================================================
  - Only processes 'Unknown Artist' folders
  - Requires 80% confidence per track
  - Requires 70% of tracks identified  
  - Requires 70% agreement on artist
  - DRY RUN by default (use --apply to make changes)
  - Searches through ALL recordings (not just first match)
============================================================
""")
    
    apply_flag = '--apply' in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    
    if apply_flag:
        print("*** APPLY MODE - Changes WILL be made ***\n")
    else:
        print("*** DRY RUN MODE - No changes will be made ***\n")
    
    if args:
        folders = [args[0]]
    else:
        if not os.path.exists(UNKNOWN_ARTIST_DIR):
            log("No 'Unknown Artist' folder found. Nothing to process.")
            return
        
        folders = [
            os.path.join(UNKNOWN_ARTIST_DIR, d)
            for d in os.listdir(UNKNOWN_ARTIST_DIR)
            if os.path.isdir(os.path.join(UNKNOWN_ARTIST_DIR, d))
        ]
    
    if not folders:
        log("No Unknown Album folders found.")
        return
    
    log(f"Found {len(folders)} folder(s) to analyze\n")
    
    for folder in folders:
        analysis, error = analyze_album(folder)
        
        if error:
            log(f"\nSkipping: {error}\n")
            continue
        
        preview_changes(analysis)
        
        if apply_flag:
            apply_changes(analysis, dry_run=False)
        else:
            log("\nTo apply these changes, run with --apply flag")


if __name__ == "__main__":
    main()
