import os
import json
import asyncio
import re
from datetime import datetime
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import MessageEntityTextUrl, InputPeerChannel
import argparse
import signal
import time

# Global flag for graceful exit
STOP_REQUESTED = False

def signal_handler(sig, frame):
    global STOP_REQUESTED
    print(f"⚠️ Signal {sig} received. Stopping fetch to save progress...")
    STOP_REQUESTED = True

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# --- Configuration ---
parser = argparse.ArgumentParser(description='Fetch Telegram News')
parser.add_argument('--channels', type=str, default='channels.txt', help='Path to channels list file')
parser.add_argument('--output', type=str, default='news.json', help='Output JSON filename (relative to frontend/public)')
parser.add_argument('--limit', type=int, default=50, help='Number of messages to check per channel')
parser.add_argument('--max-duration', type=int, default=900, help='Max duration in seconds before stopping to save (Default: 900s)')
args = parser.parse_args()

API_ID = os.environ.get("TELEGRAM_API_ID")
API_HASH = os.environ.get("TELEGRAM_API_HASH")
SESSION_STRING = os.environ.get("TELEGRAM_SESSION")

# Resolve paths
BASE_DIR = os.path.dirname(__file__)
CHANNELS_FILE = os.path.join(BASE_DIR, args.channels)
# In public repo, we want flexibility. If args.output starts with ../, it's relative to BASE_DIR
OUTPUT_FILE = os.path.join(BASE_DIR, args.output)
MEDIA_DIR = os.path.join(BASE_DIR, '../media') if '..' in args.output else os.path.join(BASE_DIR, '../frontend/public/media')

# Simple fallback for media dir to be in root if output is in root
if args.output.startswith('..'):
    MEDIA_DIR = os.path.join(BASE_DIR, '../media')

os.makedirs(MEDIA_DIR, exist_ok=True)


if not API_ID or not API_HASH:
    print("Error: TELEGRAM_API_ID and TELEGRAM_API_HASH must be set.")
    exit(1)

def clean_text(text):
    if not text:
        return ""
    # Remove markdown links [text](url)
    text = re.sub(r'\[[^\]]+\]\(https?://[^\s)]+\)', '', text)
    # Remove raw URLs in parentheses
    text = re.sub(r'\(\s*https?://[^\s)]+\s*\)', '', text)
    # Remove leftover brackets with domains or junk
    text = re.sub(r'\[\s*[a-zA-Z0-9.-]+\.[a-z]{2,}\s*\]', '', text)
    # Remove specific Telegram icons in brackets
    text = re.sub(r'\[[📹📷🖼🎥]]', '', text)
    # Remove standalone URLs
    text = re.sub(r'https?://[^\s]+', '', text)
    # Remove percent-encoded junk
    text = re.sub(r'/%[0-9A-Fa-f]{2}', '', text)
    text = re.sub(r'%[0-9A-Fa-f]{2}', '', text)
    
    # Remove specific signatures and junk
    signatures = [
        r'_+Farsi_Iranwire_+',
        r'-- _IranintlTV',
        r'VahidHeadline@ \W+',
        r'VahidOnline@ \W+',
        r'VahidOOnLine@ \W+',
        r'VahidHeadline@',
        r'VahidOnline@',
        r'VahidOOnLine@'
    ]
    for sig in signatures:
        text = re.sub(sig, '', text, flags=re.IGNORECASE)
        
    # Remove multiple newlines
    text = re.sub(r'\n\s*\n', '\n\n', text)
    return text.strip()



from PIL import Image

import subprocess

async def download_media(client, message, msg_id):
    if not message.media:
        return None, None, None
    
    # Identify media type
    is_video = False
    if hasattr(message.media, 'document'):
        mime = message.media.document.mime_type
        if mime and mime.startswith('video/'):
            is_video = True
    elif hasattr(message.media, 'video'):
        is_video = True
        
    temp_path = os.path.join(MEDIA_DIR, f"temp_{msg_id}")
    final_poster_path = os.path.join(MEDIA_DIR, f"{msg_id}_poster.jpg")
    final_video_path = os.path.join(MEDIA_DIR, f"{msg_id}.mp4")
    
    media_url = None
    poster_url = None
    media_type = 'image'

    # Check file size BEFORE download to avoid timeout on huge files
    MAX_VIDEO_SIZE_MB = 50
    file_size_bytes = 0
    if hasattr(message.media, 'document'):
        file_size_bytes = message.media.document.size
    elif hasattr(message.media, 'photo'):
        # Photos are usually small, but good to check
        pass
        
    if is_video and file_size_bytes > MAX_VIDEO_SIZE_MB * 1024 * 1024:
        print(f"⚠️ Skipping video {msg_id}: Size {file_size_bytes / (1024*1024):.2f} MB > {MAX_VIDEO_SIZE_MB} MB limit.")
        # We will still try to get the thumbnail below
        is_video = False
    
    try:
        # 1. ALWAYS download/ensure a thumbnail (poster)
        if not os.path.exists(final_poster_path):
            thumb_path = await client.download_media(message, file=temp_path, thumb=-1)
            if thumb_path and os.path.exists(thumb_path):
                try:
                    with Image.open(thumb_path) as img:
                        if img.mode in ("RGBA", "P", "CMYK"):
                            img = img.convert("RGB")
                        max_size = 1200
                        if img.width > max_size or img.height > max_size:
                            img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
                        img.save(final_poster_path, "JPEG", quality=80, optimize=True)
                    poster_url = f"/media/{msg_id}_poster.jpg"
                finally:
                    if os.path.exists(thumb_path):
                        os.remove(thumb_path)
        else:
            poster_url = f"/media/{msg_id}_poster.jpg"

        # 1.5 FFmpeg Thumbnail Fallback (If Telegram didn't have one)
        if not os.path.exists(final_poster_path) and is_video:
             # We need the video file first. We will handle this AFTER downloading the video.
             pass 


        # 2. Download and COMPRESS video
        if is_video:
            if not os.path.exists(final_video_path):
                raw_video_path = f"{temp_path}_raw.mp4"
                v_path = await client.download_media(message, file=raw_video_path)
                if v_path and os.path.exists(v_path):
                    try:
                        file_size_mb = os.path.getsize(v_path) / (1024 * 1024)
                        print(f"Processing video {msg_id} (Size: {file_size_mb:.2f} MB)...")
                        
                        # --- ADAPTIVE COMPRESSION LOGIC ---
                        if file_size_mb < 15:
                            # GREEN ZONE: Light/Standard Compression
                            # Keep 480p, decent text/visuals
                            preset = 'faster'
                            crf = '28'
                            scale = "scale='min(480,iw)':-2"
                            fps = '24'
                            audio_br = '64k'
                            print("  🟢 Green Zone: Standard compression")
                        elif file_size_mb < 50:
                            # YELLOW ZONE: Strong Compression
                            # Drop to 360p, lower quality
                            preset = 'veryfast'
                            crf = '34'
                            scale = "scale='min(360,iw)':-2"
                            fps = '20'
                            audio_br = '48k'
                            print("  🟡 Yellow Zone: Strong compression")
                        else:
                            # RED ZONE: Nuclear Compression
                            # Huge file! Aggressive shrinking needed.
                            preset = 'ultrafast'
                            crf = '40' # Very low quality
                            scale = "scale='min(360,iw)':-2"
                            fps = '15' # Choppy but small
                            audio_br = '32k'
                            print("  🔴 Red Zone: Nuclear compression")

                        cmd = [
                            'ffmpeg', '-y', '-i', v_path,
                            '-vcodec', 'libx264', '-crf', crf, '-preset', preset,
                            '-r', fps, 
                            '-acodec', 'aac', '-ac', '1', '-b:a', audio_br, '-movflags', 'faststart',
                            '-vf', scale,
                            final_video_path
                        ]
                        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                        # Final Safety Check: If still > 22MB, delete it to prevent build failure
                        if os.path.exists(final_video_path) and os.path.getsize(final_video_path) > 22 * 1024 * 1024:
                            print(f"Warning: Video {msg_id} too large ({os.path.getsize(final_video_path)} bytes) after compression, deleting.")
                            os.remove(final_video_path)
                            return None, None, None
                        
                        if os.path.exists(final_video_path):
                            media_url = f"/media/{msg_id}.mp4"
                            media_type = 'video'
                    except Exception as fe:
                        print(f"FFmpeg error for {msg_id}: {fe}")
                        # Fallback to pure thumbnail if compression fails
                        media_url = poster_url
                        media_type = 'image'
                    finally:
                        if os.path.exists(raw_video_path):
                            os.remove(raw_video_path)
            else:
                media_url = f"/media/{msg_id}.mp4"
                media_type = 'video'

            # 3. FFmpeg Poster Fallback (If Telegram didn't have one)
            # Now that we guaranteed the video file exists at final_video_path (or we failed),
            # check if we still need a poster.
            if not os.path.exists(final_poster_path) and os.path.exists(final_video_path):
                try:
                    print(f"  generating fallback poster for {msg_id}...")
                    cmd_thumb = [
                        'ffmpeg', '-y', '-i', final_video_path,
                        '-ss', '00:00:01.000', '-vframes', '1',
                        final_poster_path
                    ]
                    # If video is < 1s, try 0s
                    subprocess.run(cmd_thumb, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if os.path.exists(final_poster_path):
                        poster_url = f"/media/{msg_id}_poster.jpg"
                except Exception as e:
                    print(f"  Failed to generate fallback poster: {e}")

        else:
            # It's a photo (as before)
            final_photo_path = os.path.join(MEDIA_DIR, f"{msg_id}.jpg")
            if not os.path.exists(final_photo_path):
                p_path = await client.download_media(message, file=temp_path)
                if p_path and os.path.exists(p_path):
                    try:
                        with Image.open(p_path) as img:
                            if img.mode in ("RGBA", "P", "CMYK"):
                                img = img.convert("RGB")
                            max_size = 1200
                            if img.width > max_size or img.height > max_size:
                                img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
                            img.save(final_photo_path, "JPEG", quality=80, optimize=True)
                        media_url = f"/media/{msg_id}.jpg"
                        media_type = 'image'
                    finally:
                        if os.path.exists(p_path):
                            os.remove(p_path)
            else:
                media_url = f"/media/{msg_id}.jpg"
                media_type = 'image'
                
    except Exception as e:
        print(f"Error processing media for {msg_id}: {e}")
            
    return media_url, media_type, poster_url

async def fetch_channel_news(client, target, channel_name, limit, min_id=0):
    news_items = []
    try:
        print(f"Fetching news from {channel_name} (Target: {target}, Limit: {limit}, Min ID: {min_id})...")
        entity = await client.get_input_entity(target)
        
        count = 0
        async for message in client.iter_messages(entity, limit=limit, min_id=min_id):
            count += 1
            if not message.text and not message.media:
                continue
            
            print(f"  [{channel_name}] Processing message {count}...")
                
            msg_id = f"{channel_name}_{message.id}"
            
            # Extract link
            link = None
            if message.entities:
                for ent in message.entities:
                    if isinstance(ent, MessageEntityTextUrl):
                        link = ent.url
                        break
            
            media_path, media_type, poster_path = await download_media(client, message, msg_id)
            
            
            final_text = clean_text(message.text) if message.text else ""

            
            item = {
                "id": msg_id,
                "source": channel_name,
                "text": final_text,
                "date": message.date.isoformat(),
                "link": link if link else f"https://t.me/{channel_name}/{message.id}",
                "media": media_path,
                "mediaType": media_type,
                "poster": poster_path,
                "sensitive": getattr(message.media, 'spoiler', False)
            }
            news_items.append(item)
            
    except Exception as e:
        print(f"Error fetching from {channel_name}: {e}")
        
    return news_items

async def main():
    print(f"Starting fetch with: Channels={args.channels}, Output={args.output}, Limit={args.limit}")
    
    if not os.path.exists(CHANNELS_FILE):
        print(f"Error: Channels file not found at {CHANNELS_FILE}")
        exit(1)

    with open(CHANNELS_FILE, 'r') as f:
        raw_channels = [line.strip() for line in f if line.strip()]
        
    channels = []
    for line in raw_channels:
        parts = line.split('|')
        if len(parts) == 3:
            # Name|ID|Hash
            channels.append({
                'name': parts[0], 
                'id': int(parts[1]), 
                'hash': int(parts[2])
            })
        elif len(parts) == 2:
            # Name|ID (Legacy/Fallback)
            channels.append({
                'name': parts[0], 
                'id': int(parts[1]), 
                'hash': None
            })
        else:
            # Name only
            channels.append({'name': line, 'id': None, 'hash': None})

    # Load existing news FIRST to determine offsets
    existing_news = []
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, 'r', encoding='utf-8') as f:
                existing_news = json.load(f)
        except:
            existing_news = []

    # Calculate max ID per channel to use as min_id (High-Water Mark)
    channel_max_ids = {}
    for item in existing_news:
        if 'source' in item and 'id' in item:
            try:
                # ID format is "channel_12345"
                parts = item['id'].split('_')
                if len(parts) >= 2:
                    msg_id_num = int(parts[-1])
                    src = item['source']
                    if msg_id_num > channel_max_ids.get(src, 0):
                        channel_max_ids[src] = msg_id_num
            except:
                pass

    try:
        if SESSION_STRING:
             client = TelegramClient(StringSession(SESSION_STRING), int(API_ID), API_HASH)
        else:
             print("Error: No SESSION_STRING provided.")
             exit(1)
             
        await client.start()
        
        new_news = []
        start_time = time.time()
        
        for ch_info in channels:
            # Check for Global Timeout / Stop Signal
            if STOP_REQUESTED or (time.time() - start_time > args.max_duration):
                print(f"⏳ Time limit ({args.max_duration}s) reached or stopped. Saving partial progress...")
                break
                
            channel_name = ch_info['name']
            
            # ... (Existing channel setup) ...
            
            channel_id = ch_info['id']
            channel_hash = ch_info['hash']
            
            # Smart Sync: Fetch only messages newer than what we have
            min_id = channel_max_ids.get(channel_name, 0)
            if min_id > 0:
                print(f"🔄 Smart Sync for {channel_name}: Fetching only messages > {min_id}")
            
            # Construct Target
            target = channel_name # Default
            if channel_id and channel_hash:
                target = InputPeerChannel(channel_id=channel_id, access_hash=channel_hash)
            elif channel_id:
                target = channel_id # Might fail without hash in fresh session but try anyway

            items = await fetch_channel_news(client, target, channel_name, args.limit, min_id=min_id)
            new_news.extend(items)
            
        print(f"Fetched {len(new_news)} items from Telegram.")

        seen_ids = set()
        merged_news = []
        
        for item in new_news:
            if item['id'] not in seen_ids:
                merged_news.append(item)
                seen_ids.add(item['id'])
                
        for item in existing_news:
            if item.get('id') not in seen_ids and 'text' in item and 'date' in item:
                # We do NOT filter by source here anymore, because we want to keep
                # the existing news in this specific file intact.
                # However, if we are splitting files, each file only contains ITS sources.
                # So we SHOULD confirm the item belongs to one of the current channels?
                # Actually, no. If a channel was removed from the list but exists in json, 
                # we might want to keep it or drop it. 
                # For safety in this split-file architecture, let's keep everything currently in the file.
                merged_news.append(item)
                seen_ids.add(item['id'])
        
        merged_news.sort(key=lambda x: x['date'], reverse=True)
        # Stability: Keep only last 200 items to prevent storage overflow
        merged_news = merged_news[:200]
        
        # Cleanup orphaned media files
        # NOTE: With split files, multiple JSONs reference the same MEDIA_DIR.
        # Removing orphans based on ONE json file is DANGEROUS because another JSON might need them.
        # Solution: DISABLE strict orphan cleanup in disjoint runs, or run a separate 'cleanup' workflow.
        # For now, I will COMMENT OUT orphan cleanup to prevent deleting valid media from other streams.
        
        # active_media = set()
        # for item in merged_news:
        #     if item.get('media'):
        #         active_media.add(item['media'])
        #     if item.get('poster'):
        #         active_media.add(item['poster'])
                
        # # 1. ORPHAN CLEANUP
        # for filename in os.listdir(MEDIA_DIR):
        #     file_url = f"/media/{filename}"
        #     if file_url and file_url not in active_media and not filename.startswith("temp_"):
        #         try:
        #             os.remove(os.path.join(MEDIA_DIR, filename))
        #         except:
        #             pass
        
        # 2. GLOBAL SIZE SAFETY SWEEP (Fix for Cloudflare 25MB limit)
        # This is safe to run because it only targets oversize files which are illegal anyway.
        print("Running Global Size Safety Sweep...")
        for filename in os.listdir(MEDIA_DIR):
            file_path = os.path.join(MEDIA_DIR, filename)
            if os.path.isfile(file_path):
                try:
                    # Check if file > 22MB
                    if os.path.getsize(file_path) > 22 * 1024 * 1024: 
                        print(f"⚠️ Safety Sweep: Deleting oversized existing file {filename} ({os.path.getsize(file_path) // (1024*1024)} MB)")
                        os.remove(file_path)
                        # Also remove reference from news items to prevent broken links
                        file_web_path = f"/media/{filename}"
                        for item in merged_news:
                            if item.get('media') == file_web_path:
                                item['media'] = None
                                item['mediaType'] = None
                                # Fallback to poster if available
                                if not item.get('poster'):
                                    item['poster'] = None
                except Exception as e:
                     print(f"Error checking file size {filename}: {e}")

        os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
        with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
            json.dump(merged_news, f, ensure_ascii=False, indent=2)
            
        print(f"Successfully saved {len(merged_news)} news items (merged) to {OUTPUT_FILE}")
        
    except Exception as e:
        print(f"Critical Error: {e}")
        exit(1)
    finally:
        await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
