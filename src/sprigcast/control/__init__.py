"""Remote control: lets Streamer.bot, Stream Deck, Speaker.bot, SAMMI, Touch Portal and other apps drive
SprigCast over a WebSocket that speaks the obs-websocket 5 protocol, so they work with no changes.

protocol.py  the wire format: hello, password check, requests, batches, events (no Qt)
api.py       what each request does to SprigCast through a small Host interface, and the events
             for what changed since the last look (no Qt)
The Qt WebSocket server and the bridge to the main window live in app/control_server.py.
"""
