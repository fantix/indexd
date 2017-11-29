import uuid
import datetime

from cdispyutils.log import get_logger
from contextlib import contextmanager

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import and_
from sqlalchemy import String
from sqlalchemy import Column
from sqlalchemy import Integer
from sqlalchemy import BigInteger
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import create_engine
from sqlalchemy.orm import relationship
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.exc import NoResultFound
from sqlalchemy.orm.exc import MultipleResultsFound
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.exc import IntegrityError

from indexd.index.driver import IndexDriverABC

from indexd.index.errors import NoRecordFound
from indexd.index.errors import MultipleRecordsFound
from indexd.index.errors import RevisionMismatch
from indexd.index.errors import UnhealthyCheck
from indexd.errors import UserError
from indexd.utils import migrate_database


Base = declarative_base()

class BaseVersion(Base):
    '''
    Base index record version representation.
    '''
    __tablename__ = 'base_version'

    baseid = Column(String, primary_key=True)
    dids = relationship(
        'IndexRecord',
        backref = 'base_version',
        cascade = 'all, delete-orphan')


CURRENT_SCHEMA_VERSION = 1


class IndexSchemaVersion(Base):
    '''
    Table to track current database's schema version
    '''
    __tablename__ = 'index_schema_version'
    version = Column(Integer, primary_key=True)


class IndexRecord(Base):
    '''
    Base index record representation.
    '''
    __tablename__ = 'index_record'

    did = Column(String, primary_key=True)

    baseid = Column(String, ForeignKey('base_version.baseid'))

    rev = Column(String)

    form = Column(String)
    size = Column(BigInteger)

    last_updated = Column(DateTime, default=datetime.datetime.utcnow)

    urls = relationship(
        'IndexRecordUrl',
        backref='index_record',
        cascade='all, delete-orphan',
    )

    hashes = relationship(
        'IndexRecordHash',
        backref='index_record',
        cascade='all, delete-orphan',
    )


class IndexRecordUrl(Base):
    '''
    Base index record url representation.
    '''
    __tablename__ = 'index_record_url'

    did = Column(String, ForeignKey('index_record.did'), primary_key = True)
    url = Column(String, primary_key = True)


class IndexRecordHash(Base):
    '''
    Base index record hash representation.
    '''
    __tablename__ = 'index_record_hash'

    did = Column(String, ForeignKey('index_record.did'), primary_key = True)
    hash_type = Column(String, primary_key = True)
    hash_value = Column(String)


class SQLAlchemyIndexDriver(IndexDriverABC):
    '''
    SQLAlchemy implementation of index driver.
    '''

    def __init__(self, conn, logger=None, auto_migrate=True, **config):
        '''
        Initialize the SQLAlchemy database driver.
        '''
        self.engine = create_engine(conn, **config)
        self.logger = logger or get_logger('SQLAlchemyIndexDriver')

        Base.metadata.bind = self.engine
        Base.metadata.create_all()

        self.Session = sessionmaker(bind=self.engine)
        if auto_migrate:
            self.migrate_index_database()

    def migrate_index_database(self):
        '''
        migrate alias database to match CURRENT_SCHEMA_VERSION
        '''
        migrate_database(
            driver=self, migrate_functions=SCHEMA_MIGRATION_FUNCTIONS,
            current_schema_version=CURRENT_SCHEMA_VERSION,
            model=IndexSchemaVersion)

    @property
    @contextmanager
    def session(self):
        '''
        Provide a transactional scope around a series of operations.
        '''
        session = self.Session()

        yield session

        try:
            session.commit()
        except:
            session.rollback()
            raise
        finally:
            session.close()

    def ids(self, limit=100, start=None, size=None, urls=None, hashes=None):
        '''
        Returns list of records stored by the backend.
        '''
        # TODO add dids to filter on
        with self.session as session:
            query = session.query(IndexRecord)

            if start is not None:
                query = query.filter(IndexRecord.did > start)

            if size is not None:
                query = query.filter(IndexRecord.size == size)

            if urls is not None and urls:
                query = query.join(IndexRecord.urls)
                for u in urls:
                    query = query.filter(IndexRecordUrl.url == u)

            if hashes is not None and hashes:
                for h, v in hashes.items():
                    sub = session.query(IndexRecordHash.did)
                    sub = sub.filter(and_(
                        IndexRecordHash.hash_type == h,
                        IndexRecordHash.hash_value == v,
                    ))
                    query = query.filter(IndexRecord.did.in_(sub.subquery()))

            query = query.order_by(IndexRecord.did)
            query = query.limit(limit)

            return [i.did for i in query]

    def hashes_to_urls(self, size, hashes, start=0, limit=100):
        '''
        Returns a list of urls matching supplied size and hashes.
        '''
        with self.session as session:
            query = session.query(IndexRecordUrl)

            query = query.join(IndexRecordUrl.index_record)
            query = query.filter(IndexRecord.size == size)

            for h, v in hashes.items():
                # Select subset that matches given hash.
                sub = session.query(IndexRecordHash.did)
                sub = sub.filter(and_(
                    IndexRecordHash.hash_type == h,
                    IndexRecordHash.hash_value == v,
                ))

                # Filter anything that does not match.
                query = query.filter(IndexRecordUrl.did.in_(sub.subquery()))

            # Remove duplicates.
            query = query.distinct()


            # Return only specified window.
            query = query.offset(start)
            query = query.limit(limit)

            return [r.url for r in query]


    def add(self, form, size=None, urls=None, hashes=None, did=None, baseid=None):
        '''
        Creates a new record given urls and hashes.
        '''

        if urls is None:
            urls = []
        if hashes is None:
            hashes = {}
        with self.session as session:
            record = IndexRecord()
            base_version = None

            if(baseid == None):
                base_version = BaseVersion()
                baseid = str(uuid.uuid4())
                base_version.baseid = baseid

            record.baseid = baseid
            did = did or str(uuid.uuid4())
            record.did, record.rev = did, str(uuid.uuid4())[:8]

            record.form, record.size = form, size

            record.urls = [IndexRecordUrl(
                did=record,
                url=url,
            ) for url in urls]

            record.hashes = [IndexRecordHash(
                did=record,
                hash_type=h,
                hash_value=v,
            ) for h, v in hashes.items()]

            try:
                if(base_version):
                    session.add(base_version)
                else:
                    # check if baseid is existed in base_version table.
                    # If no, throw an exception
                    query = session.query(BaseVersion).filter(BaseVersion.baseid == baseid)
                    try:
                        query.one()
                    except NoResultFound:
                        raise UserError('{baseid} is not existed'.format(baseid=baseid), 400)

                session.add(record)
                session.commit()

            except IntegrityError as err:
                raise UserError('{did} already exists'.format(did=did), 400)

            return record.did, record.rev, record.baseid

    def get(self, did):
        '''
        Gets a record given the record id.
        '''
        with self.session as session:
            query = session.query(IndexRecord)
            query = query.filter(IndexRecord.did == did)

            try:
                record = query.one()
            except NoResultFound:
                raise NoRecordFound('no record found')
            except MultipleResultsFound:
                raise MultipleRecordsFound('multiple records found')

            baseid = record.baseid
            rev = record.rev

            form = record.form
            size = record.size

            urls = [u.url for u in record.urls]
            hashes = {h.hash_type: h.hash_value for h in record.hashes}

            last_updated = record.last_updated



        ret = {
            'did': did,
            'baseid': baseid,
            'rev': rev,
            'size': size,
            'urls': urls,
            'hashes': hashes,
            'form': form,
            "last_updated": last_updated,
        }

        return ret

    def update(self, did, rev, urls):
        '''
        Updates an existing record with new values.
        '''
        with self.session as session:
            query = session.query(IndexRecord)
            query = query.filter(IndexRecord.did == did)

            try:
                record = query.one()
            except NoResultFound:
                raise NoRecordFound('no record found')
            except MultipleResultsFound:
                raise MultipleRecordsFound('multiple records found')

            if rev != record.rev:
                raise RevisionMismatch('revision mismatch')

            if urls is not None:
                record.urls = [IndexRecordUrl(
                    did=record,
                    url=url
                ) for url in urls]

            record.rev = str(uuid.uuid4())[:8]

            session.add(record)

            return record.did, record.rev

    def delete(self, did, rev):
        '''
        Removes record if stored by backend.
        '''
        with self.session as session:
            query = session.query(IndexRecord)
            query = query.filter(IndexRecord.did == did)

            try:
                record = query.one()
            except NoResultFound:
                raise NoRecordFound('no record found')
            except MultipleResultsFound:
                raise MultipleRecordsFound('multiple records found')

            if rev != record.rev:
                raise RevisionMismatch('revision mismatch')

            session.delete(record)

    def get_all_versions(self,baseid):
        '''
        Get all records with baseid
        '''

        ret = dict()
        with self.session as session:
            query = session.query(IndexRecord)
            records = query.filter(IndexRecord.baseid == baseid).all()

            if(len(records) == 0):
                raise NoRecordFound('no record found')

            for idx, record in enumerate(records):
                rev = record.rev
                did = record.did

                form = record.form
                size = record.size

                urls = [u.url for u in record.urls]
                hashes = {h.hash_type: h.hash_value for h in record.hashes}

                last_updated= record.last_updated

                ret[idx] = {
                    'did': did,
                    'rev': rev,
                    'size': size,
                    'urls': urls,
                    'hashes': hashes,
                    'form': form,
                    'last_updated': last_updated,

                }

        return ret

    def get_latest_version(self,baseid):
        '''
        Get all records with baseid
        '''
        ret = {}
        with self.session as session:
            query = session.query(IndexRecord)
            records = query.filter(IndexRecord.baseid == baseid).order_by(IndexRecord.last_updated).all()

            if(not records):
                raise NoRecordFound('no record found')

            record = records[-1]

            rev = record.rev
            did = record.did

            form = record.form
            size = record.size

            urls = [u.url for u in record.urls]
            hashes = {h.hash_type: h.hash_value for h in record.hashes}

            last_updated = record.last_updated

            ret = {
                'did': did,
                'rev': rev,
                'size': size,
                'urls': urls,
                'hashes': hashes,
                'form': form,
                'last_updated': last_updated,
            }

        return ret


    def health_check(self):
        '''
        Does a health check of the backend.
        '''
        with self.session as session:
            try:
                query = session.execute('SELECT 1')
            except Exception as e:
                raise UnhealthyCheck()

            return True

    def __contains__(self, record):
        '''
        Returns True if record is stored by backend.
        Returns False otherwise.
        '''
        with self.session as session:
            query = session.query(IndexRecord)
            query = query.filter(IndexRecord.did == record)

            return query.exists()

    def __iter__(self):
        '''
        Iterator over unique records stored by backend.
        '''
        with self.session as session:
            for i in session.query(IndexRecord):
                yield i.did

    def totalbytes(self):
        '''
        Total number of bytes of data represented in the index.
        '''
        with self.session as session:
            result = session.execute(select([func.sum(IndexRecord.size)])).scalar()
            if result is None:
                return 0
            return long(result)

    def len(self):
        '''
        Number of unique records stored by backend.
        '''
        with self.session as session:

            return session.execute(select([func.count()]).select_from(IndexRecord)).scalar()


def migrate_1(session, **kwargs):
    session.execute(
        "ALTER TABLE {} ALTER COLUMN size TYPE bigint;"
        .format(IndexRecord.__tablename__))


# ordered schema migration functions that the index should correspond to
# CURRENT_SCHEMA_VERSION - 1 when it's written
SCHEMA_MIGRATION_FUNCTIONS = [migrate_1]
