#!/usr/bin/env python
from collections import defaultdict
import glob
from multiprocessing.dummy import Pool as thread_pool
import os
import sys
import datetime
import time
import gzip

from sqlalchemy import create_engine

from sqlalchemy import Table, Column, Integer, String, MetaData, DateTime, desc
metadata = MetaData()
#web
from bottle import route, run, template, Bottle

''' originally by @JustinAzoff, adjusted to my particular situation:
    gzipped bro logs fetched daily, etc. '''


dns_table = Table('dns', metadata,
    Column('query', String(255), primary_key=True, index=True),
    Column('type', String(16), primary_key=True),
    Column('answer', String(255), primary_key=True, index=True),
    Column('count', Integer),
    Column('ttl', Integer),
    Column('first', DateTime),
    Column('last', DateTime),

    # Add constraint
    UniqueConstraint('query', 'type', 'answer', name='QTA_1'),
)


def hashfile(filepath):
    sha1 = hashlib.sha1()
    f = open(filepath, 'rb')add
    try:
        sha1.update(f.read())
    finally:
        f.close()
    return sha1.hexdigest()



def reader(f):
    line = ''
    headers = {}
    it = iter(f)
    while not line.startswith("#types"):
        line = next(it).rstrip()
        k,v = line[1:].split(None, 1)
        headers[k] = v

    sep = headers['separator'].decode("string-escape")

    for k,v in headers.items():
        if sep in v:
            headers[k] = v.split(sep)

    headers['separator'] = sep
    fields = headers['fields']
    types = headers['types']
    set_sep = headers['set_separator']

    vectors = [field for field, type in zip(fields, types) if type.startswith("vector[")]

    for row in it:
        if row.startswith("#close"): break
        parts = row.rstrip().split(sep)
        rec = dict(zip(fields, parts))
        for f in vectors:
            rec[f] = rec[f].split(set_sep)
        yield rec

ts = datetime.datetime.fromtimestamp

class SQLStore:
    def __init__(self, db_uri=None):
        uri = db_uri
        if not uri:
            uri = os.getenv("BRO_PDNS_DB")
        if not uri:
            raise RuntimeError("db_uri is not set. set BRO_PDNS_DB environment variable perhaps?")

        self.engine = engine = create_engine(uri, pool_recycle=3600)
        metadata.create_all(engine)
        self.conn = engine.connect()

        self._select = dns_table.select()
        self._insert = dns_table.insert()
        self._update = dns_table.update()

    def close(self):
        self.conn.close()

    def upsert_record(self,add query, type, answer, ttl, timestamp, count):
        d = dns_table.c
        n = ts(int(float(timestamp)))
        ttl = ttl != "-" and int(float(ttl)) or None

        q = self._select.where( (d.query == query) & (d.type == type) & (d.answer == answer))
        ret = self.conn.execute(q)
        #print ret

        upsert_ok = False
        tries = 0
        retry_limit = 10

        if ret.rowcount:
            record = map(fixup_no_date, ret)[0]
            if ret.rowcount != 1: print 'WARNING: query returned %d results '% ret.rowcount

            if n > record['last']:
                    q = self._update.where( (d.query == query) & (d.type == type) & (d.answer == answer)).values(
                        count=d.count+1,
                        last=n,
                        ttl=ttl
                    )
            elif n < record['first']:
                    q = self._update.where( (d.query == query) & (d.type == type) & (d.answer == answer)).values(
                        count=d.count+1,
                        first=n,
                        ttl=ttl
                    )

            while not upsert_ok and tries <= retry_limit:
                try:
                    ret = self.conn.execute(q)
                    upsert_ok = True
                except:
                    # TODO Fix upserts lost because of deadlocks
                    print 'Deadlock detected while updating, sleeping 1'
                    tries += 1
                    time.sleep(1)
                    pass

        else:
            while not upsert_ok and tries <= retry_limit:
                try:
                    self.conn.execute(self._insert.values(query=query, type=type, answer=answer, ttl=ttl, count=count, first=n, last=n))
                    upsert_ok = True
                except:
                    print 'Deadlock detected while inserting, sleeping 1'
                    tries += 1
                    time.sleep(1)
                    pass


    def begin(self):
        self._trans = self.conn.begin()

    def commit(self):
        self._trans.commit()

    def search(self, q):
        d = dns_table.c
        records = self.engine.execute(
            self._select.where((d.query == q) | (d.answer == q)).order_by(desc(d.last))
        ).fetchall()
        return records

    def search_full(self, q):
        d = dns_table.c
        q = '%' + q + '%'
        records = self.engine.execute(
            self._select.where(d.query.like(q) | d.answer.like(q)).order_by(desc(d.last))
        ).fetchall()
        return records


def aggregate_file(f):
    pairs = defaultdict(int)
    ttls = {}
    times = {}
    try:
        the_file = gzip.open(f, 'rb')
    except:
        the_file = open(f)

    for rec in reader(the_file):
        q = rec['query']
        # we don't want unknown queries, they're useless to us... artifact of bro logging only response

        if q != '-':
            t = rec['qtype_name']
            for a, ttl in zip(rec['answers'], rec['TTLs']):
                tup = (q,t,a)
                pairs[tup] += 1
                ttls[tup] = ttl
                times[tup] = rec["ts"]


    for tup, count in pairs.iteritems():
        (q,t,a) = tup
        #print "q=%s t=%s a=%s c=%s" % (q,t,a,count)
        ttl = ttls[tup]
        time = times[tup]
        yield {
            "query": q,
            "type": t,
            "answer": a,
            "ttl": ttl,
            "timestamp": time,
            "count": count,
        }

SIZE_TIMEOUT = 5
def is_growing(f):
    size = os.stat(f).st_size
    time.sleep(0.1)
    for x in range(SIZE_TIMEOUT):
        time.sleep(1)
        newsize = os.stat(f).st_size
        if newsize != size:
            return True
    return False

def window(i,slice=5):
    for x in xrange(0,len(i),slice):
        a=x
        b=x+slice
        yield i[a:b]

def load_records(records):
    store = SQLStore()
    store.begin()

    for rec in sorted(records):
        store.upsert_record(**rec)
    store.commit()
    store.close()
    return len(records)

def process_fn(f):
    thread_count = int(os.getenv("BRO_PDNS_THREADS", "1"))
    print 'Spawning %d threads' % thread_count
    print 'Processing: %s' % f
    processed = 0

    aggregated = list(aggregate_file(f))
    batches = window(aggregated, 10000)

    pool = thread_pool(thread_count)

    processed = sum(pool.imap(load_records, batches, chunksize=1))

    print "%d records processed\n" % processed

def add_to_processed(file_hash):
    with open('processed_hashes.txt', 'a') as myfile:
        myfile.write("%s\n" % file_hash)

def is_processed(file_hash):
    try:
        with open('processed_hashes.txt', 'r') as myfile:
            hashlist = myfile.read()

        if file_hash in hashlist:
            return True
        else:
            return False
    except:
        return False

def process():
    f = sys.argv[2]
    file_hash = hashfile(f)
    if not is_processed(file_hash):
        process_fn(f)
        add_to_processed(file_hash)

def from_file():
    f = sys.argv[2]
    if not is_processed(f):
        process_fn(f)
        add_to_processed(f)

def watch():
    pattern = sys.argv[2]
    while True:
        files = glob.glob(pattern)
        not_growing = (f for f in files if not is_growing(f))
        for fn in not_growing:
            process_fn(fn)
            os.unlink(fn)
        if not files:
            time.sleep(5)

#api

# Note, this fixup converts dates to strings
def fixup(record):
    r = dict(record)
    for x in 'first', 'last':
        r[x] = str(r[x])
    return r
# Note, this fixup does not convert dates to strings
def fixup_no_date(record):
    r = dict(record)
    return r

app = Bottle()
@app.route('/dns/<q>')
def dns_search(q):
    records = app.db.search(q)
    records = map(fixup, records)
    return { "records": records }

@app.route('/dns/full/<q>')
def dns_search(q):
    records = app.db.search_full(q)
    records = map(fixup, records)
    return { "records": records }

def serve():
    app.db = SQLStore()
    app.run(host='0.0.0.0', port=1337)

MAPPING = {
    "process": process,
    "watch": watch,
    "serve": serve,
}

if __name__ == "__main__":
    try :
        action = sys.argv[1]
        func = MAPPING[action]
    except (IndexError, KeyError):
        print "Usage: %s [process foo.log(.gz)] | [watch '/path/to/dns*.log'] | [serve]" % sys.argv[0]
        sys.exit(1)

    func()
