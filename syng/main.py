from flask import jsonify, request, render_template
import subprocess
from threading import Thread, Lock
import pafy
import shlex
import os.path

from . import app, db, args, extensions, auth
from .database import Artists, Songs, Albums
from .synctools import PreviewQueue, locked, ReaderWriterLock
from .scanner import rough_scan, update
from .tags import Tags
from .appname import appname_pretty, version
from .youtube_wrapper import search

class Entry(dict):
    def __init__(self, id, singer, type="library"):
        super().__init__()
        self.id = id
        self['singer'] = singer
        self['type'] = type
        if type == "library":
            with app.rwlock.locked_for_read():
                song = Songs.query.filter(Songs.id == id).one_or_none()
            if song is not None:
                self['title'] = song.title
                self['artist'] = song.artist.name
                self['album'] = song.album.title
                self['duration'] = song.duration
                self.path = song.path
            if song.only_initial:
                tagext = song.type
                if 'audioext' in extensions[song.type]:
                    tagext = extensions[song.type]['audioext']
                meta = Tags("%s.%s" % (song.path[:-4], tagext))
                self['title'] = meta.title
                self['artist'] = meta.artist
                self['album'] = meta.album
                self['duration'] = meta.duration
        elif type == "youtube":
            song = pafy.new(id)
            self['title'] = song.title
            self['artist'] = song.author
            self['album'] = "YouTube"
            self.path = song.getbest().url
            self['duration'] = song.length

    def from_dict(d):
        if not 'type' in d:
            d['type'] = "library"
        return Entry(d['id'], d['singer'], d['type'])

@app.route('/comments', methods=['GET'])
def get_comments():
    song = request.args.get("song")
    with app.rwlock.locked_for_read():
        comments = db.Comments.query.filter(db.Comments.song_id == song).all()
    return jsonify(result = [{'name': comment.name, 'comment': comment.comment} for comment in comments])

@app.route('/comments', methods=['POST'])
def post_comment():
    json = request.get_json(force=True)


@app.route('/query', methods=['GET'])
def query():
    args = request.args
    qtype = args.get("type")
    query = args.get("q")
    res = []
    if qtype == "library":
        with app.rwlock.locked_for_read():
            title = Songs.query.filter(Songs.title.like("%%%s%%" % query)).all()
            artists = Songs.query.join(Artists.query.filter(Artists.name.like("%%%s%%" % query))).all()
            res = [r.to_dict() for r in set(title + artists)]
    elif qtype == "youtube":
        channel = args.get("channel")
        if channel == None:
            print(query)
            res = search(query)


    return jsonify(result = res, request=request.args)

@app.route('/queue', methods=['GET'])
def get_queue():
    queue = app.queue.get_list()
    return jsonify(current = app.current, queue = queue, last10 = app.last10)

@app.route('/queue', methods=['POST'])
def append_queue():
    json = request.get_json(force=True)
    content = Entry.from_dict(json)
    app.queue.put(content)
    queue = app.queue.get_list()
    return jsonify(current = app.current, queue = queue, last10 = app.last10)

@app.route('/queue', methods=['PATCH'])
@auth.required
def alter_queue():
    json = request.get_json(force=True)
    action = json["action"]
    if action == "skip":
        app.process.terminate()
    if action == "delete":
        index = json["param"]["index"]
        app.queue.delete(index)
    elif action == "move":
        src = json["param"]["src"]
        dst = json["param"]["dst"]
        app.queue.move(src, dst)
    queue = app.queue.get_list()
    return jsonify(current = app.current, queue = queue, last10 = app.last10)

@app.route('/admin', methods=['GET'])
@auth.required
def admin_index():
    return render_template("index.html", admin=True, appname=appname_pretty, version=version)

@app.route('/', methods=['GET'])
def index():
    return render_template("index.html", appname=appname_pretty, version=version)

def enquote(string):
    return "\"%s\"" % string

class MPlayerThread(Thread):
    def __init__(self, app):
        super().__init__()
        self.app = app

    def run(self):
        while True:
            app.current = self.app.queue.get()
            print(app.current)
            if app.current['type'] == "library":
                title, ext = os.path.splitext(app.current.path)
                ext = ext[1:]
                player = app.configuration['default']['player']
                if 'player' in app.configuration[ext]:
                    player = app.configuration[ext]['player']

                command = app.configuration['playback'][player]
                try:
                    fullcommand = command.format(video=enquote(app.current.path))
                except KeyError:
                    fullcommand = command.format(video=enquote(app.current.path),
                                                 audio="\"%s.%s\"" % (title, app.configuration[ext]['audioext']))

                app.process = subprocess.Popen(shlex.split(fullcommand))
                app.process.wait()
                rc = app.process.returncode
                if rc != 0:
                    print("ERROR!")
            elif app.current['type'] == "youtube":
                player = app.configuration['default']['player']
                if 'player' in app.configuration['youtube']:
                    player = app.configuration['youtube']['player']
                command = app.configuration['playback'][player]
                fullcommand = command.format(video=enquote(app.current.path))
                app.process = subprocess.Popen(shlex.split(fullcommand))
                app.process.wait()
                rc = app.process.returncode
                if rc != 0:
                    print("ERROR!")
            else:
                print("DOES NOT COMPUTE")
            app.last10 = app.last10[:9]
            app.last10.insert(0,app.current)
            app.current = None


class ScannerThread(Thread):
    def __init__(self, path, db, extensions, rwlock):
        super().__init__()
        self.library = path
        self.db = db
        self.extensions = extensions
        self.rwlock = rwlock

    def run(self):
        update(self.library, self.db, self.extensions, self.rwlock)

def main():
    app.rwlock = ReaderWriterLock()
    app.current = None
    app.last10 = []

    db.create_all()
    if args.scan:
        rough_scan(app.configuration['library']['path'], extensions, db) # Initial fast scan
        scannerThread = ScannerThread(app.configuration['library']['path'], db, extensions, app.rwlock)
        scannerThread.start()
    app.queue = PreviewQueue()
    mpthread = MPlayerThread(app)
    mpthread.start()
    app.run(port=int(app.configuration['server']['port']), host=app.configuration['server']['host'])


if __name__ == '__main__':
    main()
