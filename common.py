#!/usr/bin/python
# -*- coding: utf-8 -*-
# -----------------------------------------------------------------------
#    This file is part of WAPT
#    Copyright (C) 2013  Tranquil IT Systems http://www.tranquil.it
#    WAPT aims to help Windows systems administrators to deploy
#    setup and update applications on users PC.
#
#    WAPT is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    WAPT is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with WAPT.  If not, see <http://www.gnu.org/licenses/>.
#
# -----------------------------------------------------------------------
from waptutils import __version__

import os
import re
import logging
import datetime
import time
import sys
import tempfile
import hashlib
import glob
import codecs
import base64
import zlib
import sqlite3
import json
import StringIO
import requests
import cPickle
try:
    # pylint: disable=no-member
    # no error
    import requests.packages.urllib3
    requests.packages.urllib3.disable_warnings()
except:
    pass

import fnmatch
import platform
import socket
import ssl
import copy
import getpass
import psutil
import threading
import traceback
import uuid

import gc

import random
import string

import locale

import shlex
from iniparse import RawConfigParser
from optparse import OptionParser

from collections import namedtuple
from collections import OrderedDict
from types import ModuleType

import shutil
import urlparse
import zipfile

# Windows stuff
import windnsquery
import win32api
import ntsecuritycon
import win32security
import win32net
import pywintypes
from ntsecuritycon import DOMAIN_GROUP_RID_ADMINS,DOMAIN_GROUP_RID_USERS

import ctypes
from ctypes import wintypes

logger = logging.getLogger()

try:
    import requests_kerberos
    has_kerberos = True
except:
    has_kerberos = False

from _winreg import HKEY_LOCAL_MACHINE,EnumKey,OpenKey,QueryValueEx,\
    EnableReflectionKey,DisableReflectionKey,QueryReflectionKey,\
    QueryInfoKey,DeleteValue,DeleteKey,\
    KEY_READ,KEY_WOW64_32KEY,KEY_WOW64_64KEY,KEY_ALL_ACCESS

# end of windows stuff

from waptutils import BaseObjectClass,ensure_list,ensure_unicode,default_http_headers
from waptutils import httpdatetime2isodate,datetime2isodate,FileChunks,jsondump,ZipFile
from waptutils import import_code,import_setup,force_utf8_no_bom,format_bytes,wget,merge_dict,remove_encoding_declaration,list_intersection

from waptcrypto import SSLCABundle,SSLCertificate,SSLPrivateKey,SSLCRL,SSLVerifyException
from waptcrypto import get_peer_cert_chain_from_server,default_pwd_callback,hexdigest_for_data
from waptcrypto import sha256_for_data,EWaptMissingPrivateKey,EWaptMissingCertificate

from waptpackage import EWaptException,EWaptMissingLocalWaptFile,EWaptNotAPackage,EWaptNotSigned
from waptpackage import EWaptBadTargetOS,EWaptNeedsNewerAgent,EWaptDiskSpace
from waptpackage import EWaptUnavailablePackage,EWaptConflictingPackage
from waptpackage import EWaptDownloadError

from waptpackage import REGEX_PACKAGE_CONDITION,WaptRemoteRepo,PackageEntry

import setuphelpers
import netifaces


class EWaptBadServerAuthentication(EWaptException):
    pass

def is_system_user():
    return setuphelpers.get_current_user() == 'system'


###########################"
class LogInstallOutput(BaseObjectClass):
    """file like to log print output to db installstatus"""
    def __init__(self,console,waptdb,rowid):
        self.output = []
        self.console = console
        self.waptdb = waptdb
        self.rowid = rowid
        self.threadid = threading.current_thread()
        self.lock = threading.RLock()

    def write(self,txt):
        with self.lock:
            txt = ensure_unicode(txt)
            try:
                self.console.write(txt)
            except:
                self.console.write(repr(txt))
            if txt != '\n':
                self.output.append(txt)
                if txt and txt[-1] != u'\n':
                    txtdb = txt+u'\n'
                else:
                    txtdb = txt
                if threading.current_thread() == self.threadid:
                    self.waptdb.update_install_status(self.rowid,'RUNNING',txtdb if not txtdb == None else None)

    def writing(self):
        with self.lock:
            return False

    def __getattrib__(self, name):
        if hasattr(self.console,'__getattrib__'):
            return self.console.__getattrib__(name)
        else:
            return self.console.__getattribute__(name)


##################
def ipv4_to_int(ipaddr):
    (a,b,c,d) = ipaddr.split('.')
    return (int(a) << 24) + (int(b) << 16) + (int(c) << 8) + int(d)


def same_net(ip1,ip2,netmask):
    """Given 2 ipv4 address and mask, return True if in same subnet"""
    return (ipv4_to_int(ip1) & ipv4_to_int(netmask)) == (ipv4_to_int(ip2) & ipv4_to_int(netmask))


def host_ipv4():
    """return a list of (iface,mac,{addr,broadcast,netmask})"""
    ifaces = netifaces.interfaces()
    res = []
    for i in ifaces:
        params = netifaces.ifaddresses(i)
        if netifaces.AF_LINK in params and params[netifaces.AF_LINK][0]['addr'] and not params[netifaces.AF_LINK][0]['addr'].startswith('00:00:00'):
            iface = {'iface':i,'mac':params[netifaces.AF_LINK][0]['addr']}
            if netifaces.AF_INET in params:
                iface.update(params[netifaces.AF_INET][0])
            res.append( iface )
    return res


def tryurl(url,proxies=None,timeout=2,auth=None,verify_cert=False,cert=None):
    # try to get header for the supplied URL, returns None if no answer within the specified timeout
    # else return time to get he answer.
    try:
        logger.debug(u'  trying %s' % url)
        starttime = time.time()
        headers = requests.head(url=url,
            proxies=proxies,
            timeout=timeout,
            auth=auth,
            verify=verify_cert,
            headers=default_http_headers(),
            cert=cert)
        if headers.ok:
            logger.debug(u'  OK')
            return time.time() - starttime
        else:
            headers.raise_for_status()
    except Exception as e:
        logger.debug(u'  Not available : %s' % ensure_unicode(e))
        return None

class EWaptCancelled(Exception):
    pass


class WaptBaseDB(BaseObjectClass):
    _dbpath = ''
    _db_version = None
    db = None
    curr_db_version = None

    def __init__(self,dbpath):
        self.transaction_depth = 0
        self._db_version = None
        self.dbpath = dbpath
        self.threadid = None

    @property
    def dbpath(self):
        return self._dbpath

    @dbpath.setter
    def dbpath(self,value):
        if not self._dbpath or (self._dbpath and self._dbpath != value):
            self._dbpath = value
            self.connect()

    def begin(self):
        # recreate a connection if not in same thread (reuse of object...)
        if self.threadid is not None and self.threadid != threading.current_thread().ident:
            logger.warning('Reset of DB connection, reusing wapt db object in a new thread')
            self.connect()
        elif self.threadid is None:
            self.connect()
        if self.transaction_depth == 0:
            logger.debug(u'DB Start transaction')
            self.db.execute('begin')
        self.transaction_depth += 1

    def commit(self):
        if self.transaction_depth > 0:
            self.transaction_depth -= 1
        if self.transaction_depth == 0:
            logger.debug(u'DB commit')
            try:
                self.db.execute('commit')
            except:
                self.db.execute('rollback')
                raise

    def rollback(self):
        if self.transaction_depth > 0:
            self.transaction_depth -= 1
        if self.transaction_depth == 0:
            logger.debug(u'DB rollback')
            self.db.execute('rollback')

    def connect(self):
        if not self.dbpath:
            return
        logger.debug('Thread %s is connecting to wapt db' % threading.current_thread().ident)
        self.threadid = threading.current_thread().ident
        if not self.dbpath == ':memory:' and not os.path.isfile(self.dbpath):
            dirname = os.path.dirname(self.dbpath)
            if os.path.isdir (dirname)==False:
                os.makedirs(dirname)
            os.path.dirname(self.dbpath)
            self.db=sqlite3.connect(self.dbpath,detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
            self.db.isolation_level = None
            self.transaction_depth = 0
            self.initdb()
        elif self.dbpath == ':memory:':
            self.db=sqlite3.connect(self.dbpath,detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
            self.db.isolation_level = None
            self.transaction_depth = 0
            self.initdb()
        else:
            self.db=sqlite3.connect(self.dbpath,detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
            self.db.isolation_level = None
            self.transaction_depth = 0
            if self.curr_db_version != self.db_version:
                self.upgradedb()

    def __enter__(self):
        self.begin()
        #logger.debug(u'DB enter %i' % self.transaction_depth)
        return self

    def __exit__(self, type, value, tb):
        if not value:
            #logger.debug(u'DB exit %i' % self.transaction_depth)
            self.commit()
        else:
            self.rollback()
            logger.debug(u'Error at DB exit %s, rollbacking\n%s' % (value,ensure_unicode(traceback.format_tb(tb))))

    @property
    def db_version(self):
        if not self._db_version:
            val = self.db.execute('select value from wapt_params where name="db_version"').fetchone()
            if val:
                self._db_version = val[0]
            else:
                raise Exception('Unknown DB Version')
        return self._db_version

    @db_version.setter
    def db_version(self,value):
        with self:
            self.db.execute('insert or replace into wapt_params(name,value,create_date) values (?,?,?)',('db_version',value,datetime2isodate()))
            self._db_version = value

    @db_version.deleter
    def db_version(self):
        with self:
            self.db.execute("delete from wapt_params where name = 'db_version'")
            self._db_version = None

    def initdb(self):
        pass

    def set_param(self,name,value):
        """Store permanently a (name/value) pair in database, replace existing one"""
        with self:
            self.db.execute('insert or replace into wapt_params(name,value,create_date) values (?,?,?)',(name,value,datetime2isodate()))

    def get_param(self,name,default=None):
        """Retrieve the value associated with name from database"""
        q = self.db.execute('select value from wapt_params where name=? order by create_date desc limit 1',(name,)).fetchone()
        if q:
            return q[0]
        else:
            return default

    def delete_param(self,name):
        with self:
            self.db.execute('delete from wapt_params where name=?',(name,))

    def query(self,query, args=(), one=False,as_dict=True):
        """
        execute la requete query sur la db et renvoie un tableau de dictionnaires
        """
        cur = self.db.execute(query, args)
        if as_dict:
            rv = [dict((cur.description[idx][0], value)
                   for idx, value in enumerate(row)) for row in cur.fetchall()]
        else:
            rv = cur.fetchall()
        return (rv[0] if rv else None) if one else rv


    def upgradedb(self,force=False):
        """Update local database structure to current version if rules are described in db_upgrades

        Args:
            force (bool): force upgrade even if structure version is greater than requested.

        Returns:
            tuple: (old_structure_version,new_structure_version)

        """
        with self:
            try:
                backupfn = ''
                # use cached value to avoid infinite loop
                old_structure_version = self._db_version
                if old_structure_version >= self.curr_db_version and not force:
                    logger.warning(u'upgrade db aborted : current structure version %s is newer or equal to requested structure version %s' % (old_structure_version,self.curr_db_version))
                    return (old_structure_version,old_structure_version)

                logger.info(u'Upgrade database schema')
                if self.dbpath != ':memory:':
                    # we will backup old data in a file so that we can rollback
                    backupfn = tempfile.mktemp('.sqlite')
                    logger.debug(u' copy old data to %s' % backupfn)
                    shutil.copy(self.dbpath,backupfn)
                else:
                    backupfn = None

                # we will backup old data in dictionaries to convert them to new structure
                logger.debug(u' backup data in memory')
                old_datas = {}
                tables = [ c[0] for c in self.db.execute('SELECT name FROM sqlite_master WHERE type = "table" and name like "wapt_%"').fetchall()]
                for tablename in tables:
                    old_datas[tablename] = self.query('select * from %s' % tablename)
                    logger.debug(u' %s table : %i records' % (tablename,len(old_datas[tablename])))

                logger.debug(u' drop tables')
                for tablename in tables:
                    self.db.execute('drop table if exists %s' % tablename)

                # create new empty structure
                logger.debug(u' recreates new tables ')
                new_structure_version = self.initdb()
                del(self.db_version)
                # append old data in new tables
                logger.debug(u' fill with old data')
                for tablename in tables:
                    if old_datas[tablename]:
                        logger.debug(u' process table %s' % tablename)
                        allnewcolumns = [ c[0] for c in self.db.execute('select * from %s limit 0' % tablename).description]
                        # take only old columns which match a new column in new structure
                        oldcolumns = [ k for k in old_datas[tablename][0] if k in allnewcolumns ]

                        insquery = "insert into %s (%s) values (%s)" % (tablename,",".join(oldcolumns),",".join("?" * len(oldcolumns)))
                        for rec in old_datas[tablename]:
                            logger.debug(u' %s' %[ rec[oldcolumns[i]] for i in range(0,len(oldcolumns))])
                            self.db.execute(insquery,[ rec[oldcolumns[i]] for i in range(0,len(oldcolumns))] )

                # be sure to put back new version in table as db upgrade has put the old value in table
                self.db_version = new_structure_version
                return (old_structure_version,new_structure_version)
            except Exception as e:
                if backupfn:
                    logger.critical(u"UpgradeDB ERROR : %s, copy back backup database %s" % (e,backupfn))
                    shutil.copy(backupfn,self.dbpath)
                raise

class WaptSessionDB(WaptBaseDB):
    curr_db_version = '20161103'

    def __init__(self,username=''):
        super(WaptSessionDB,self).__init__(None)
        if not username:
            username = setuphelpers.get_current_user()
        self.username = username
        self.dbpath = os.path.join(setuphelpers.application_data(),'wapt','waptsession.sqlite')

    def initdb(self):
        """Initialize current sqlite db with empty table and return structure version"""
        assert(isinstance(self.db,sqlite3.Connection))
        logger.debug(u'Initialize Wapt session database')

        self.db.execute("""
        create table if not exists wapt_sessionsetup (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          username varchar(255),
          package varchar(255),
          version varchar(255),
          architecture varchar(255),
          install_date varchar(255),
          install_status varchar(255),
          install_output TEXT,
          process_id integer
          )"""
                        )
        self.db.execute("""
            create index if not exists idx_sessionsetup_username on wapt_sessionsetup(username,package);""")

        self.db.execute("""
        create table if not exists wapt_params (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name  varchar(64),
          value text,
          create_date varchar(255)
          ) """)

        self.db.execute("""
          create unique index if not exists idx_params_name on wapt_params(name);
          """)
        self.db_version = self.curr_db_version
        return self.curr_db_version

    def add_start_install(self,package,version,architecture):
        """Register the start of installation in local db
        """
        with self:
            cur = self.db.execute("""delete from wapt_sessionsetup where package=?""" ,(package,))
            cur = self.db.execute("""\
                  insert into wapt_sessionsetup (
                    username,
                    package,
                    version,
                    architecture,
                    install_date,
                    install_status,
                    install_output,
                    process_id
                    ) values (?,?,?,?,?,?,?,?)
                """,(
                     self.username,
                     package,
                     version,
                     architecture,
                     datetime2isodate(),
                     'INIT',
                     '',
                     os.getpid()
                   ))
            return cur.lastrowid

    def update_install_status(self,rowid,install_status,install_output):
        """Update status of package installation on localdb"""
        with self:
            if install_status in ('OK','ERROR'):
                pid = None
            else:
                pid = os.getpid()
            cur = self.db.execute("""\
                  update wapt_sessionsetup
                    set install_status=?,install_output = install_output || ?,process_id=?
                    where rowid = ?
                """,(
                     install_status,
                     install_output,
                     pid,
                     rowid,
                     )
                   )
            return cur.lastrowid

    def update_install_status_pid(self,pid,install_status='ERROR'):
        """Update status of package installation on localdb"""
        with self:
            cur = self.db.execute("""\
                  update wapt_sessionsetup
                    set install_status=? where process_id = ?
                """,(
                     install_status,
                     pid,
                     )
                   )
            return cur.lastrowid

    def remove_install_status(self,package):
        """Remove status of package installation from localdb

        >>> wapt = Wapt()
        >>> wapt.forget_packages('tis-7zip')
        ???
        """
        with self:
            cur = self.db.execute("""delete from wapt_sessionsetup where package=?""" ,(package,))
            return cur.rowcount

    def remove_obsolete_install_status(self,installed_packages):
        """Remove local user status of packages no more installed"""
        with self:
            cur = self.db.execute("""delete from wapt_sessionsetup where package not in (%s)"""%\
                ','.join('?' for i in installed_packages), installed_packages)
            return cur.rowcount

    def is_installed(self,package,version):
        p = self.query('select * from  wapt_sessionsetup where package=? and version=? and install_status="OK"',(package,version))
        if p:
            return p[0]
        else:
            return None


PackageKey = namedtuple('package',('packagename','version'))

class WaptDB(WaptBaseDB):
    """Class to manage SQLite database with local installation status"""

    curr_db_version = '20180303'

    def initdb(self):
        """Initialize current sqlite db with empty table and return structure version"""
        assert(isinstance(self.db,sqlite3.Connection))
        logger.debug(u'Initialize Wapt database')
        self.db.execute("""
        create table if not exists wapt_package (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          package varchar(255),
          version varchar(255),
          architecture varchar(255),
          section varchar(255),
          priority varchar(255),
          maintainer varchar(255),
          description varchar(255),
          filename varchar(255),
          size integer,
          md5sum varchar(255),
          depends varchar(800),
          conflicts varchar(800),
          sources varchar(255),
          repo_url varchar(255),
          repo varchar(255),
          signer varchar(255),
          signer_fingerprint varchar(255),
          signature varchar(255),
          signature_date varchar(255),
          signed_attributes varchar(800),
          min_wapt_version varchar(255),
          maturity varchar(255),
          locale varchar(255),
          installed_size integer,
          target_os varchar(255),
          max_os_version varchar(255),
          min_os_version varchar(255),
          impacted_process varchar(255)
        )"""
                        )
        self.db.execute("""
        create index if not exists idx_package_name on wapt_package(package);""")

        self.db.execute("""
        create table if not exists wapt_localstatus (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          package varchar(255),
          version varchar(255),
          version_pinning varchar(255),
          explicit_by varchar(255),
          architecture varchar(255),
          maturity varchar(255),
          locale varchar(255),
          install_date varchar(255),
          install_status varchar(255),
          install_output TEXT,
          install_params VARCHAR(800),
          uninstall_string varchar(255),
          uninstall_key varchar(255),
          setuppy TEXT,
          process_id integer,
          depends varchar(800),
          conflicts varchar(800),
          last_audit_on varchar(255),
          last_audit_status varchar(255),
          last_audit_output TEXT,
          next_audit_on varchar(255),
          impacted_process varchar(255)
          )
          """)
        self.db.execute("""
        create index if not exists idx_localstatus_name on wapt_localstatus(package);""")

        self.db.execute("""
        create table if not exists wapt_params (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name  varchar(64),
          value text,
          create_date varchar(255)
          ) """)

        self.db.execute("""
          create unique index if not exists idx_params_name on wapt_params(name);
          """)

        # action : install, remove, check, session_setup, update, upgrade
        # state : draft, planned, postponed, running, done, error, canceled
        self.db.execute("""
            CREATE TABLE if not exists wapt_task (
                id integer NOT NULL PRIMARY KEY AUTOINCREMENT,
                action varchar(16),
                state varchar(16),
                current_step varchar(255),
                process_id integer,
                start_date varchar(255),
                finish_date varchar(255),
                package_name varchar(255),
                username varchar(255),
                package_version_min varchar(255),
                package_version_max varchar(255),
                rundate_min varchar(255),
                rundate_max varchar(255),
                rundate_nexttry varchar(255),
                runduration_max integer,
                created_date varchar(255),
                run_params VARCHAR(800),
                run_output TEXT
            );
                """)

        self.db.execute("""
          create index if not exists idx_task_state on wapt_task(state);
          """)

        self.db.execute("""
          create index if not exists idx_task_package_name on wapt_task(package_name);
          """)

        self.db.execute("""
        create table if not exists wapt_sessionsetup (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          username varchar(255),
          package varchar(255),
          version varchar(255),
          architecture varchar(255),
          maturity varchar(255),
          locale varchar(255),
          install_date varchar(255),
          install_status varchar(255),
          install_output TEXT
          )"""
                        )
        self.db.execute("""
        create index idx_sessionsetup_username on wapt_sessionsetup(username,package);""")

        self.db_version = self.curr_db_version
        return self.curr_db_version

    def add_package(self,
                    package='',
                    version='',
                    section='',
                    priority='',
                    architecture='',
                    maintainer='',
                    description='',
                    filename='',
                    size='',
                    md5sum='',
                    depends='',
                    conflicts='',
                    sources='',
                    repo_url='',
                    repo='',
                    signer='',
                    signer_fingerprint='',
                    maturity='',
                    locale='',
                    signature='',
                    signature_date='',
                    signed_attributes='',
                    min_wapt_version='',
                    installed_size=None,
                    max_os_version='',
                    min_os_version='',
                    target_os='',
                    impacted_process='',
                    ):

        with self:
            cur = self.db.execute("""\
                  insert into wapt_package (
                    package,
                    version,
                    section,
                    priority,
                    architecture,
                    maintainer,
                    description,
                    filename,
                    size,
                    md5sum,
                    depends,
                    conflicts,
                    sources,
                    repo_url,
                    repo,
                    signer,
                    signer_fingerprint,
                    maturity,
                    locale,
                    signature,
                    signature_date,
                    signed_attributes,
                    min_wapt_version,
                    installed_size,
                    max_os_version,
                    min_os_version,
                    target_os,
                    impacted_process
                    ) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,(
                     package,
                     version,
                     section,
                     priority,
                     architecture,
                     maintainer,
                     description,
                     filename,
                     size,
                     md5sum,
                     depends,
                     conflicts,
                     sources,
                     repo_url,
                     repo,
                     signer,
                     signer_fingerprint,
                     maturity,
                     locale,
                     signature,
                     signature_date,
                     signed_attributes,
                     min_wapt_version,
                     installed_size,
                     max_os_version,
                     min_os_version,
                     target_os,
                     impacted_process
                     )
                   )
            return cur.lastrowid

    def add_package_entry(self,package_entry,locale_code=None):
        cur = self.db.execute("""delete from wapt_package where package=? and version=? and architecture=? and maturity=? and locale=?""" ,
            (package_entry.package,package_entry.version,package_entry.architecture,package_entry.maturity,package_entry.locale))

        with self:
            self.add_package(package=package_entry.package,
                             version=package_entry.version,
                             section=package_entry.section,
                             priority=package_entry.priority,
                             architecture=package_entry.architecture,
                             maintainer=package_entry.maintainer,
                             description=package_entry.get_localized_description(locale_code),
                             filename=package_entry.filename,
                             size=package_entry.size,
                             md5sum=package_entry.md5sum,
                             depends=package_entry.depends,
                             conflicts=package_entry.conflicts,
                             sources=package_entry.sources,
                             repo_url=package_entry.repo_url,
                             repo=package_entry.repo,
                             signer=package_entry.signer,
                             signer_fingerprint=package_entry.signer_fingerprint,
                             maturity=package_entry.maturity,
                             locale=package_entry.locale,
                             signature=package_entry.signature,
                             signature_date=package_entry.signature_date,
                             signed_attributes=package_entry.signed_attributes,
                             min_wapt_version=package_entry.min_wapt_version,
                             installed_size=package_entry.installed_size,
                             max_os_version=package_entry.max_os_version,
                             min_os_version=package_entry.min_os_version,
                             target_os=package_entry.target_os,
                             impacted_process=package_entry.impacted_process
                             )

    def add_start_install(self,package,version,architecture,params_dict={},explicit_by=None,maturity='',locale='',depends='',conflicts='',impacted_process=None):
        """Register the start of installation in local db

        Args:
            params_dict (dict) : dictionary of parameters provided on command line with --param or by the server
            explicit_by (str) : username of initiator of the install.
                          if not None, install is not a dependencie but an explicit manual install
            setuppy (str) : python source code used for install, uninstall or session_setup
                            code used for uninstall or session_setup must use only wapt self library as
                            package content is no longer available at this step.
        """
        with self:
            cur = self.db.execute("""delete from wapt_localstatus where package=?""" ,(package,))
            cur = self.db.execute("""\
                  insert into wapt_localstatus (
                    package,
                    version,
                    architecture,
                    install_date,
                    install_status,
                    install_output,
                    install_params,
                    explicit_by,
                    process_id,
                    maturity,
                    locale,
                    depends,
                    conflicts,
                    impacted_process
                    ) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,(
                     package,
                     version,
                     architecture,
                     datetime2isodate(),
                     'INIT',
                     '',
                     jsondump(params_dict),
                     explicit_by,
                     os.getpid(),
                     maturity,
                     locale,
                     depends,
                     conflicts,
                     impacted_process
                   ))
            return cur.lastrowid

    def update_install_status(self,rowid,install_status,install_output,uninstall_key=None,uninstall_string=None):
        """Update status of package installation on localdb"""
        with self:
            if install_status in ('OK','ERROR'):
                pid = None
            else:
                pid = os.getpid()
            cur = self.db.execute("""\
                  update wapt_localstatus
                    set install_status=?,install_output = install_output || ?,uninstall_key=?,uninstall_string=?,process_id=?
                    where rowid = ?
                """,(
                     install_status,
                     install_output,
                     uninstall_key,
                     uninstall_string,
                     pid,
                     rowid,
                     )
                   )
            return cur.lastrowid

    def update_install_status_pid(self,pid,install_status='ERROR'):
        """Update status of package installation on localdb"""
        with self:
            cur = self.db.execute("""\
                  update wapt_localstatus
                    set install_status=? where process_id = ?
                """,(
                     install_status,
                     pid,
                     )
                   )
            return cur.lastrowid

    def switch_to_explicit_mode(self,package,user_id):
        """Set package install mode to manual
        so that package is not removed
        when meta packages don't require it anymore
        """
        with self:
            cur = self.db.execute("""\
                  update wapt_localstatus
                    set explicit_by=? where package = ?
                """,(
                     user_id,
                     package,
                     )
                   )
            return cur.lastrowid

    def store_setuppy(self,rowid,setuppy=None,install_params={}):
        """Update status of package installation on localdb"""
        with self:
            cur = self.db.execute("""\
                  update wapt_localstatus
                    set setuppy=?,install_params=? where rowid = ?
                """,(
                     remove_encoding_declaration(setuppy),
                     jsondump(install_params),
                     rowid,
                     )
                   )
            return cur.lastrowid

    def remove_install_status(self,package):
        """Remove status of package installation from localdb"""
        with self:
            cur = self.db.execute("""delete from wapt_localstatus where package=?""" ,(package,))
            return cur.rowcount

    def known_packages(self):
        """return a list of all (package,version)"""
        q = self.db.execute("""\
              select distinct wapt_package.package,wapt_package.version from wapt_package
           """)
        return [PackageKey(*e) for e in q.fetchall()]

    def packages_matching(self,package_cond):
        """Return an ordered list of available packages entries which match
        the condition "packagename[([=<>]version)]?"
        version ascending
        """
        pcv_match = REGEX_PACKAGE_CONDITION.match(package_cond)
        if pcv_match:
            pcv = pcv_match.groupdict()
            q = self.query_package_entry("""\
                  select * from wapt_package where package = ?
               """, (pcv['package'],))
            result = [ p for p in q if p.match(package_cond)]
            result.sort()
            return result
        else:
            return []

    def packages_search(self,searchwords=[],exclude_host_repo=True,section_filter=None):
        """Return a list of package entries matching the search words"""
        if not isinstance(searchwords,list) and not isinstance(searchwords,tuple):
            searchwords = [searchwords]
        if not searchwords:
            words = []
            search = [u'1=1']
        else:
            words = [ u"%"+w.lower()+"%" for w in searchwords ]
            search = [u"lower(description || package) like ?"] *  len(words)
        if exclude_host_repo:
            search.append(u'repo <> "wapt-host"')
        if section_filter:
            section_filter = ensure_list(section_filter)
            search.append(u'section in ( %s )' %  u",".join(['"%s"' % x for x in  section_filter]))

        result = self.query_package_entry(u"select * from wapt_package where %s" % " and ".join(search),words)
        result.sort()
        return result

    def installed(self,include_errors=False):
        """Return a dict of installed packages on this host

        Args:
            include_errors (bool) : if False, only packages with status 'OK' and 'UNKNOWN' are returned
                                    if True, all packages are installed.

        Returns:
            dict: installed packages keys=package, values = PackageEntry
        """
        sql = ["""\
              select l.package,l.version,l.architecture,l.install_date,l.install_status,l.install_output,l.install_params,l.explicit_by,
                l.depends,l.conflicts,
                r.section,r.priority,r.maintainer,r.description,r.sources,r.filename,r.size,
                r.repo_url,r.md5sum,r.repo,l.maturity,l.locale
                from wapt_localstatus l
                left join wapt_package r on r.package=l.package and l.version=r.version and
                    (l.architecture is null or l.architecture=r.architecture) and
                    (l.maturity is null or l.maturity=r.maturity) and
                    (l.locale is null or l.locale=r.locale)
           """]
        if not include_errors:
            sql.append('where l.install_status in ("OK","UNKNOWN")')

        q = self.query_package_entry('\n'.join(sql))
        result = {}
        for p in q:
            result[p.package]= p
        return result

    def install_status(self,id):
        """Return a PackageEntry of the local install status for id

        Args:
            id: sql rowid
        """
        sql = ["""\
              select l.package,l.version,l.architecture,l.install_date,l.install_status,l.install_output,l.install_params,l.explicit_by,l.setuppy,
                l.depends,l.conflicts,
                r.section,r.priority,r.maintainer,r.description,r.sources,r.filename,r.size,
                r.repo_url,r.md5sum,r.repo,l.maturity,l.locale
                from wapt_localstatus l
                left join wapt_package r on
                    r.package=l.package and l.version=r.version and
                    (l.architecture is null or l.architecture=r.architecture) and
                    (l.maturity is null or l.maturity=r.maturity) and
                    (l.locale is null or l.locale=r.locale)
                where l.id = ?
           """]

        q = self.query_package_entry('\n'.join(sql),args = [id])
        if q:
            return q[0]
        else:
            return None

    def installed_search(self,searchwords=[],include_errors=False):
        """Return a list of installed package entries based on search keywords"""
        if not isinstance(searchwords,list) and not isinstance(searchwords,tuple):
            searchwords = [searchwords]
        if not searchwords:
            words = []
            search = ['1=1']
        else:
            words = [ u"%"+w.lower()+"%" for w in searchwords ]
            search = [u"lower(l.package || (case when r.description is NULL then '' else r.description end) ) like ?"] *  len(words)
        if not include_errors:
            search.append(u'l.install_status in ("OK","UNKNOWN")')
        q = self.query_package_entry(u"""\
              select l.package,l.version,l.architecture,l.install_date,l.install_status,l.install_output,l.install_params,l.explicit_by,
                l.depends,l.conflicts,
                r.section,r.priority,r.maintainer,r.description,r.sources,r.filename,r.size,
                r.repo_url,r.md5sum,r.repo
                 from wapt_localstatus l
                left join wapt_package r on r.package=l.package and l.version=r.version and (l.architecture is null or l.architecture=r.architecture)
              where %s
           """ % " and ".join(search),words)
        return q

    def installed_matching(self,package_cond,include_errors=False):
        """Return True if one properly installed (if include_errors=False) package match the package condition 'tis-package (>=version)' """
        package = REGEX_PACKAGE_CONDITION.match(package_cond).groupdict()['package']
        if include_errors:
            status = '"OK","UNKNOWN","ERROR"'
        else:
            status = '"OK","UNKNOWN"'

        q = self.query_package_entry(u"""\
              select l.package,l.version,l.architecture,l.install_date,l.install_status,l.install_output,l.install_params,l.setuppy,l.explicit_by,
                l.depends,l.conflicts,
                r.section,r.priority,r.maintainer,r.description,r.sources,r.filename,r.size,
                r.repo_url,r.md5sum,r.repo
                from wapt_localstatus l
                left join wapt_package r on r.package=l.package and l.version=r.version and (l.architecture is null or l.architecture=r.architecture)
              where l.package=? and l.install_status in (%s)
           """ % status,(package,))
        return q[0] if q and q[0].match(package_cond) else None

    def upgradeable(self,include_errors=True):
        """Return a dictionary of upgradable Package entries"""
        result = {}
        allinstalled = self.installed(include_errors=True).values()
        for p in allinstalled:
            available = self.query_package_entry("""select * from wapt_package where package=?""",(p.package,))
            available.sort()
            available.reverse()
            if available and (available[0] > p) or (include_errors and (p.install_status == 'ERROR')):
                result[p.package] = available
        return result

    def build_depends(self,packages):
        """Given a list of packages conditions (packagename (optionalcondition))
        return a list of dependencies (packages conditions) to install

        TODO : choose available dependencies in order to reduce the number of new packages to install

        >>> waptdb = WaptDB(':memory:')
        >>> office = PackageEntry('office','0')
        >>> firefox22 = PackageEntry('firefox','22')
        >>> firefox22.depends = 'mymissing,flash'
        >>> firefox24 = PackageEntry('firefox','24')
        >>> thunderbird = PackageEntry('thunderbird','23')
        >>> flash10 = PackageEntry('flash','10')
        >>> flash12 = PackageEntry('flash','12')
        >>> office.depends='firefox(<24),thunderbird,mymissing'
        >>> firefox22.depends='flash(>=10)'
        >>> firefox24.depends='flash(>=12)'
        >>> waptdb.add_package_entry(office)
        >>> waptdb.add_package_entry(firefox22)
        >>> waptdb.add_package_entry(firefox24)
        >>> waptdb.add_package_entry(flash10)
        >>> waptdb.add_package_entry(flash12)
        >>> waptdb.add_package_entry(thunderbird)
        >>> waptdb.build_depends('office')
        ([u'flash(>=10)', u'firefox(<24)', u'thunderbird'], [u'mymissing'])
        """
        if not isinstance(packages,list) and not isinstance(packages,tuple):
            packages = [packages]

        MAXDEPTH = 30
        # roots : list of initial packages to avoid infinite loops

        def dodepends(explored,packages,depth,missing):
            if depth>MAXDEPTH:
                raise Exception('Max depth in build dependencies reached, aborting')
            alldepends = []
            # loop over all package names
            for package in packages:
                if not package in explored:
                    entries = self.packages_matching(package)
                    if not entries:
                        missing.append(package)
                    else:
                        # get depends of the most recent matching entry
                        # TODO : use another older if this can limit the number of packages to install !
                        depends =  ensure_list(entries[-1].depends)
                        available_depends = []
                        for d in depends:
                            if self.packages_matching(d):
                                available_depends.append(d)
                            else:
                                missing.append(d)
                        alldepends.extend(dodepends(explored,available_depends,depth+1,missing))
                        for d in available_depends:
                            if not d in alldepends:
                                alldepends.append(d)
                    explored.append(package)
            return alldepends

        missing = []
        explored = []
        depth = 0
        alldepends = dodepends(explored,packages,depth,missing)
        return (alldepends,missing)

    def package_entry_from_db(self,package,version_min='',version_max=''):
        """Return the most recent package entry given its packagename and minimum and maximum version

        >>> waptdb = WaptDB(':memory:')
        >>> waptdb.add_package_entry(PackageEntry('dummy','1'))
        >>> waptdb.add_package_entry(PackageEntry('dummy','2'))
        >>> waptdb.add_package_entry(PackageEntry('dummy','3'))
        >>> waptdb.package_entry_from_db('dummy')
        PackageEntry('dummy','3')
        >>> waptdb.package_entry_from_db('dummy',version_min=2)
        PackageEntry('dummy','3')
        >>> waptdb.package_entry_from_db('dummy',version_max=1)
        PackageEntry('dummy','1')
        """
        result = PackageEntry()
        filter = ""
        if version_min is None:
            version_min=""
        if version_max is None:
            version_max=""

        if not version_min and not version_max:
            entries = self.query("""select * from wapt_package where package = ? order by version desc limit 1""",(package,))
        else:
            entries = self.query("""select * from wapt_package where package = ? and (version>=? or ?="") and (version<=? or ?="") order by version desc limit 1""",
                (package,version_min,version_min,version_max,version_max))
        if not entries:
            raise Exception('Package %s (min : %s, max %s) not found in local DB, please update' % (package,version_min,version_max))
        for k,v in entries[0].iteritems():
            setattr(result,k,v)
        return result

    def query_package_entry(self,query, args=(), one=False):
        """Execute la requete query sur la db et renvoie un tableau de PackageEntry

        Le matching est fait sur le nom de champs.
        Les champs qui ne matchent pas un attribut de PackageEntry
        sont également mis en attributs !

        >>> waptdb = WaptDB(':memory:')
        >>> waptdb.add_package_entry(PackageEntry('toto','0',repo='main'))
        >>> waptdb.add_package_entry(PackageEntry('dummy','2',repo='main'))
        >>> waptdb.add_package_entry(PackageEntry('dummy','1',repo='main'))
        >>> waptdb.query_package_entry("select * from wapt_package where package=?",["dummy"])
        [PackageEntry('dummy','2'), PackageEntry('dummy','1')]
        >>> waptdb.query_package_entry("select * from wapt_package where package=?",["dummy"],one=True)
        PackageEntry('dummy','2')
        """
        result = []
        cur = self.db.execute(query, args)
        for row in cur.fetchall():
            pe = PackageEntry()
            rec_dict = dict((cur.description[idx][0], value) for idx, value in enumerate(row))
            for k in rec_dict:
                setattr(pe,k,rec_dict[k])
                # add joined field to calculated attributes list
                if not k in pe.all_attributes:
                    pe._calculated_attributes.append(k)
            result.append(pe)
        if one and result:
            result = sorted(result)[-1]
        return result

    def purge_repo(self,repo_name):
        """remove references to repo repo_name

        >>> waptdb = WaptDB('c:/wapt/db/waptdb.sqlite')
        >>> waptdb.purge_repo('main')
        """
        with self:
            self.db.execute('delete from wapt_package where repo=?',(repo_name,))

    def params(self,packagename):
        """Return install parameters associated with a package"""
        with self:
            cur = self.db.execute("""select install_params from wapt_localstatus where package=?""" ,(packagename,))
            rows = cur.fetchall()
            if rows:
                return json.loads(rows[0][0])

class WaptServer(BaseObjectClass):
    """Manage connection to waptserver"""

    def __init__(self,url=None,proxies={'http':None,'https':None},timeout = 2,dnsdomain=None):
        if url and url[-1]=='/':
            url = url.rstrip('/')
        self._server_url = url
        self._cached_dns_server_url = None

        self.proxies=proxies
        self.timeout = timeout
        self.use_kerberos = False
        self.verify_cert = True

        self.client_certificate = None
        self.client_private_key = None

        self.interactive_session = False
        self.ask_user_password_hook = None

        if dnsdomain:
            self.dnsdomain = dnsdomain
        else:
            self.dnsdomain = setuphelpers.get_domain_fromregistry()

    def get_computer_principal(self):
        try:
            dnsdomain = setuphelpers.get_domain_fromregistry()
            if not dnsdomain:
                dnsdomain = self.dnsdomain

            return '%s@%s' % (setuphelpers.get_computername().upper(),dnsdomain.upper())
        except Exception as e:
            logger.critical('Unable to build computer_principal %s' % repr(e))
            raise

    def auth(self,action=None):
        if self._server_url:
            if action in ('add_host_kerberos','add_host'):
                scheme = urlparse.urlparse(self._server_url).scheme
                if scheme == 'https' and has_kerberos and self.use_kerberos:
                    return requests_kerberos.HTTPKerberosAuth(mutual_authentication=requests_kerberos.DISABLED)

                    # TODO : simple auth if kerberos is not available...
                else:
                    return self.ask_user_password(action)
            else:
                return self.ask_user_password(action)
        else:
            return None

    def save_server_certificate(self,server_ssl_dir=None,overwrite=False):
        """Retrieve certificate of https server for further checks

        Args:
            server_ssl_dir (str): Directory where to save x509 certificate file

        Returns:
            str : full path to x509 certificate file.

        """
        certs = get_peer_cert_chain_from_server(self.server_url)
        if certs:
            new_cert = certs[0]
            url = urlparse.urlparse(self.server_url)
            pem_fn = os.path.join(server_ssl_dir,url.hostname+'.crt')

            if new_cert.cn != url.hostname:
                logger.warning('Warning, certificate CN %s sent by server does not match URL host %s' % (new_cert.cn,url.hostname))

            if not os.path.isdir(server_ssl_dir):
                os.makedirs(server_ssl_dir)
            if os.path.isfile(pem_fn):
                try:
                    # compare current and new cert
                    old_cert = SSLCertificate(pem_fn)
                    if old_cert.modulus != new_cert.modulus:
                        if not overwrite:
                            raise Exception('Can not save server certificate, a file with same name but from diffrent key already exists in %s' % pem_fn)
                        else:
                            logger.info('Overwriting old server certificate %s with new one %s'%(old_cert.fingerprint,new_cert.fingerprint))
                    return pem_fn
                except Exception as e:
                    logger.critical('save_server_certificate : %s'% repr(e))
                    raise
            # write full chain
            open(pem_fn,'wb').write('\n'.join(cert.as_pem() for cert in certs))
            logger.info('New certificate %s with fingerprint %s saved to %s'%(new_cert,new_cert.fingerprint,pem_fn))
            return pem_fn
        else:
            return None

    def reset_network(self):
        """called by wapt when network configuration has changed"""
        self._cached_dns_server_url = None

    @property
    def server_url(self):
        """Return fixed url if any, else request DNS

        >>> server = WaptServer(timeout=4)
        >>> print server.dnsdomain
        tranquilit.local
        >>> server = WaptServer(timeout=4)
        >>> print server.dnsdomain
        tranquilit.local
        >>> print server.server_url
        https://wapt.tranquil.it
        """
        if self._server_url is not None:
            return self._server_url
        else:
            if not self._cached_dns_server_url:
                try:
                    self._cached_dns_server_url = self.find_wapt_server_url()
                except Exception:
                    logger.debug('DNS server is not available to get waptserver URL')
            return self._cached_dns_server_url

    def find_wapt_server_url(self):
        """Search the WAPT server with dns SRV query

        preference for SRV is :
           same priority asc -> weight desc

        >>> WaptServer(dnsdomain='tranquilit.local',timeout=4,url=None).server_url
        'https://wapt.tranquilit.local'
        >>> WaptServer(url='http://srvwapt:8080',timeout=4).server_url
        'http://srvwapt:8080'
        """

        try:
            if self.dnsdomain and self.dnsdomain != '.':
                # find by dns SRV _waptserver._tcp
                try:
                    logger.debug(u'Trying _waptserver._tcp.%s SRV records' % self.dnsdomain)
                    answers = windnsquery.dnsquery_srv('_waptserver._tcp.%s' % self.dnsdomain)
                    servers = []
                    for (priority,weight,wapthost,port) in answers:
                        # get first numerical ipv4 from SRV name record
                        try:
                            if port == 443:
                                url = 'https://%s' % (wapthost)
                                servers.append((priority,-weight,url))
                            else:
                                url = 'http://%s:%i' % (wapthost,port)
                                servers.append((priority,-weight,url))
                        except Exception as e:
                            logging.debug('Unable to resolve : error %s' % (ensure_unicode(e),))

                    if servers:
                        servers.sort()
                        logger.debug(u'  Defined servers : %s' % (servers,))
                        return servers[0][2]

                    if not answers:
                        logger.debug(u'  No _waptserver._tcp.%s SRV record found' % self.dnsdomain)
                except Exception as e:
                    logger.debug(u'  DNS resolver exception _SRV records: %s' % (ensure_unicode(e),))
                    raise

            else:
                logger.warning(u'Local DNS domain not found, skipping SRV _waptserver._tcp search ')

            return None
        except Exception as e:
            logger.debug(u'WaptServer.find_wapt_server_url: DNS resolver exception: %s' % (e,))
            raise

    @server_url.setter
    def server_url(self,value):
        """Wapt main repository URL
        """
        # remove / at the end
        if value:
            value = value.rstrip('/')
        self._server_url = value

    def load_config(self,config,section='global'):
        """Load waptserver configuration from inifile
        """
        if not section:
            section = 'global'
        if config.has_section(section):
            if config.has_option(section,'wapt_server'):
                # if defined but empty, look in dns srv
                url = config.get(section,'wapt_server')
                if url:
                    self._server_url = url
                else:
                    self._server_url = None
            else:
                # no server at all
                self._server_url = ''

            if  config.has_option(section,'use_kerberos'):
                self.use_kerberos =  config.getboolean(section,'use_kerberos')

            if config.has_option(section,'use_http_proxy_for_server') and config.getboolean(section,'use_http_proxy_for_server'):
                if config.has_option(section,'http_proxy'):
                    self.proxies = {'http':config.get(section,'http_proxy'),'https':config.get(section,'http_proxy')}
                else:
                    self.proxies = None
            else:
                self.proxies = {'http':None,'https':None}

            if config.has_option(section,'wapt_server_timeout'):
                self.timeout = config.getfloat(section,'wapt_server_timeout')

            if config.has_option(section,'dnsdomain'):
                self.dnsdomain = config.get(section,'dnsdomain')

            if config.has_option(section,'verify_cert'):
                try:
                    self.verify_cert = config.getboolean(section,'verify_cert')
                except:
                    self.verify_cert = config.get(section,'verify_cert')
                    if self.verify_cert == '':
                        self.verify_cert = '0'
                    elif not os.path.isfile(self.verify_cert):
                        logger.warning(u'waptserver certificate %s declared in configuration file can not be found. Waptserver communication will fail' % self.verify_cert)

        return self

    def load_config_from_file(self,config_filename,section='global'):
        """Load waptserver configuration from an inifile located at config_filename

        Args:
            config_filename (str) : path to wapt inifile
            section (str): ini section from which to get parameters. default to 'global'

        Returns:
            WaptServer: self

        """
        ini = RawConfigParser()
        ini.read(config_filename)
        self.load_config(ini,section)
        return self

    def get(self,action,auth=None,timeout=None):
        """ """
        surl = self.server_url
        if surl:
            req = requests.get("%s/%s" % (surl,action),
                proxies=self.proxies,verify=self.verify_cert,
                timeout=timeout or self.timeout,auth=auth,
                cert=self.client_auth(),
                headers=default_http_headers(),
                allow_redirects=True)
            if req.status_code == 401:
                req = requests.get("%s/%s" % (surl,action),
                    proxies=self.proxies,verify=self.verify_cert,
                    timeout=timeout or self.timeout,auth=self.auth(action=action),
                    headers=default_http_headers(),
                    allow_redirects=True)

            req.raise_for_status()
            return json.loads(req.content)
        else:
            raise Exception(u'Wapt server url not defined or not found in DNS')

    def post(self,action,data=None,files=None,auth=None,timeout=None,signature=None,signer=None,content_length=None):
        """Post data to waptserver using http POST method

        Add a signature to the posted data using host certificate.

        Posted Body is gzipped

        Args:
            action (str): doc part of the url
            data (str) : posted data body
            files (list or dict) : list of filenames

        """
        surl = self.server_url
        if surl:
            headers = default_http_headers()
            if data:
                headers.update({
                    'Content-type': 'binary/octet-stream',
                    'Content-transfer-encoding': 'binary',
                    })
                if isinstance(data,str):
                    headers['Content-Encoding'] = 'gzip'
                    data = zlib.compress(data)

            if signature:
                headers.update({
                    'X-Signature': base64.b64encode(signature),
                    })
            if signer:
                headers.update({
                    'X-Signer': signer,
                    })

            if content_length is not None:
                headers['Content-Length'] = "%s" % content_length

            if isinstance(files,list):
                files_dict = {}
                for fn in files:
                    with open(fn,'rb') as f:
                        files_dict[os.path.basename(fn)] = f.read()
            elif isinstance(files,dict):
                files_dict = files
            else:
                files_dict = None

            # check if auth is required before sending data in chunk
            retry_count=0
            if files_dict:
                while True:
                    req = requests.head("%s/%s" % (surl,action),
                            proxies=self.proxies,
                            verify=self.verify_cert,
                            timeout=timeout or self.timeout,
                            headers=headers,
                            auth=auth,
                            cert=self.client_auth(),
                            allow_redirects=True)
                    if req.status_code == 401:
                        retry_count += 1
                        if retry_count >= 3:
                            raise EWaptBadServerAuthentication('Authentication failed on server %s for action %s' % (self.server_url,action))
                        auth = self.auth(action=action)
                    else:
                        break

            while True:
                req = requests.post("%s/%s" % (surl,action),
                    data=data,
                    files=files_dict,
                    proxies=self.proxies,
                    verify=self.verify_cert,
                    timeout=timeout or self.timeout,
                    auth=auth,
                    cert=self.client_auth(),
                    headers=headers,
                    allow_redirects=True)

                if (req.status_code == 401) and (retry_count < 3):
                    retry_count += 1
                    if retry_count >= 3:
                        raise EWaptBadServerAuthentication('Authentication failed on server %s for action %s' % (self.server_url,action))
                    auth = self.auth(action=action)
                else:
                    break
            req.raise_for_status()
            return json.loads(req.content)
        else:
            raise Exception(u'Wapt server url not defined or not found in DNS')

    def client_auth(self):
        """Return SSL pair (cert,key) filenames for client side SSL auth
        """
        if self.client_certificate and os.path.isfile(self.client_certificate) and os.path.isfile(self.client_private_key):
            return (self.client_certificate,self.client_private_key)
        else:
            return None


    def available(self):
        try:
            if self.server_url:
                req = requests.head("%s/ping" % (self.server_url),proxies=self.proxies,
                    verify=self.verify_cert,
                    timeout=self.timeout,
                    auth=None,
                    cert=self.client_auth(),
                    headers=default_http_headers(),
                    allow_redirects=True)
                if req.status_code == 401:
                    req = requests.head("%s/ping" % (self.server_url),proxies=self.proxies,
                        verify=self.verify_cert,
                        timeout=self.timeout,
                        auth=self.auth(action='ping'),
                        cert=self.client_auth(),
                        headers=default_http_headers(),
                        allow_redirects=True)
                req.raise_for_status()
                return True
            else:
                logger.debug(u'Wapt server is unavailable because no URL is defined')
                return False
        except Exception as e:
            logger.debug(u'Wapt server %s unavailable because %s'%(self._server_url,ensure_unicode(e)))
            return False

    def as_dict(self):
        result = {}
        attributes = ['server_url','proxies','dnsdomain']
        for att in attributes:
            result[att] = getattr(self,att)
        return result

    def upload_packages(self,packages,auth=None,timeout=None,progress_hook=None):
        """Upload a list of PackageEntry with local wapt build/signed files
        Returns:
            dict: {'ok','errors'} list of http post upload results
        """
        packages = ensure_list(packages)
        files = {}

        ok = []
        errors = []

        for package in packages:
            if not isinstance(package,PackageEntry):
                pe = PackageEntry().load_control_from_wapt(package)
                package_filename = package
            else:
                pe = package
                package_filename = pe.localpath

            # TODO : issue if more hosts to upload than allowed open file handles.
            if pe.localpath and os.path.isfile(pe.localpath):
                if pe.section in ['host','group','unit']:
                    # small local files, don't stream, we will upload many at once with form encoded files
                    files[os.path.basename(package_filename)] = open(pe.localpath,'rb').read()
                else:
                    # stream it immediately
                    logger.debug('Uploading %s to server %s' % (pe.localpath,self.server_url))
                    res = self.post('api/v3/upload_packages',data = FileChunks(pe.localpath,progress_hook=progress_hook).get(),auth=auth,timeout=300)
                    if not res['success']:
                        errors.append(res)
                        logger.critical('Error when uploading package %s: %s'% (pe.localpath, res['msg']))
                    else:
                        ok.append(res)
            elif pe._package_content is not None:
                # cached package content for hosts
                files[os.path.basename(package_filename)] = pe._package_content
            else:
                raise EWaptMissingLocalWaptFile('No content to upload for %s' % pe.asrequirement())

        if files:
            try:
                logger.debug('Uploading %s files to server %s'% (len(files),self.server_url))
                res = self.post('api/v3/upload_packages',files=files,auth=auth,timeout=300)
                if not res['success']:
                    errors.append(res)
                    logger.critical('Error when uploading packages: %s'% (res['msg']))
                else:
                    ok.append(res)
            finally:
                pass
        return dict(ok=ok,errors=errors)


    def ask_user_password(self,action=None):
        """Ask for basic auth if server requires it"""
        if self.ask_user_password_hook is not None:
            return self.ask_user_password_hook(action) # pylint: disable=not-callable
        elif self.interactive_session:
            user = raw_input('Please get login for action "%s" on server %s: ' % (action,self.server_url))
            if user:
                password = getpass.getpass('Password: ')
                if user and password:
                    return (user,password)
                else:
                    return None
        else:
            return None

    def __repr__(self):
        try:
            return '<WaptServer %s>' % self.server_url
        except:
            return '<WaptServer %s>' % 'unknown'


class WaptRepo(WaptRemoteRepo):
    """Gives access to a remote http repository, with a zipped Packages packages index
    Find its repo_url based on
    * repo_url explicit setting in ini config section [<name>]
    * dnsdomain: if repo_url is empty, lookup a _<name>._tcp.<dnsdomain> SRV record

    >>> repo = WaptRepo(name='main',url='http://wapt/wapt',timeout=4)
    >>> packages = repo.packages()
    >>> len(packages)
    """

    def __init__(self,url=None,name='wapt',verify_cert=None,http_proxy=None,timeout = 2,dnsdomain=None,cabundle=None,config=None):
        """Initialize a repo at url "url".

        Args:
            name (str): internal local name of this repository
            url  (str): http URL to the repository.
                 If url is None, the url is requested from DNS by a SRV query
            http_proxy (str): URL to http proxy or None if no proxy.
            timeout (float): timeout in seconds for the connection to the rmeote repository
            dnsdomain (str): DNS domain to use for autodiscovery of URL if url is not supplied.

        .. versionchanged:: 1.4.0
           authorized_certs (list):  list of trusted SSL certificates to filter out untrusted entries.
                                 if None, no check is performed. All antries are accepted.
        .. versionchanged:: 1.5.0
           cabundle (SSLCABundle):  list of trusted SSL ca certificates to filter out untrusted entries.
                                     if None, no check is performed. All antries are accepted.

        """

        # additional properties
        self._default_config.update({
            'dnsdomain':'',
        })

        # create additional properties
        self._dnsdomain = None
        self._cached_dns_repo_url = None

        WaptRemoteRepo.__init__(self,url=url,name=name,verify_cert=verify_cert,http_proxy=http_proxy,timeout=timeout,cabundle=cabundle,config=config)

        # force with supplied not None parameters
        if dnsdomain is not None:
            self.dnsdomain = dnsdomain

    def reset_network(self):
        """called by wapt when network configuration has changed"""
        self._cached_dns_repo_url = None
        self._packages = None
        self._packages_date = None

    @property
    def dnsdomain(self):
        return self._dnsdomain

    @dnsdomain.setter
    def dnsdomain(self,value):
        if value != self._dnsdomain:
            self._dnsdomain = value
            self._cached_dns_repo_url = None

    @property
    def repo_url(self):
        """Repository URL

        Fixed url if any, else request DNS with a SRV _wapt._tcp.domain query
        or a CNAME by the find_wapt_repo_url method.

        The URL is queried once and then cached into a local property.

        Returns:
            str: url to the repository

        >>> repo = WaptRepo(name='wapt',timeout=4)
        >>> print repo.dnsdomain
        tranquilit.local
        >>> repo = WaptRepo(name='wapt',timeout=4)
        >>> print repo.dnsdomain
        tranquilit.local
        >>> print repo.repo_url
        http://srvwapt.tranquilit.local/wapt
        """
        if self._repo_url:
            return self._repo_url
        else:
            if not self._cached_dns_repo_url and self.dnsdomain:
                self._cached_dns_repo_url = self.find_wapt_repo_url()
            elif not self.dnsdomain:
                raise Exception(u'No dnsdomain defined for repo %s'%self.name)
            return self._cached_dns_repo_url

    @repo_url.setter
    def repo_url(self,value):
        if value:
            value = value.rstrip('/')

        if value != self._repo_url:
            self._repo_url = value
            self._packages = None
            self._packages_date = None
            self._cached_dns_repo_url = None


    def find_wapt_repo_url(self):
        """Search the nearest working main WAPT repository given the following priority
        - URL defined in ini file
        - first SRV record in the same network as one of the connected network interface
        - first SRV record with the highest weight
        - wapt CNAME in the local dns domain (https first then http)

        Preference for SRV records is :
           same subnet -> priority asc -> weight desc

        Returns:
            str: URL to the server.

        >>> repo = WaptRepo(name='wapt',dnsdomain='tranquil.it',timeout=4,url=None)
        >>> repo.repo_url
        'http://wapt.tranquil.it./wapt'
        >>> repo = WaptRepo(name='wapt',url='http://wapt/wapt',timeout=4)
        >>> repo.repo_url
        'http://wapt/wapt'
        """

        try:
            local_ips = socket.gethostbyname_ex(socket.gethostname())[2]
            logger.debug(u'All interfaces : %s' % [ "%s/%s" % (i['addr'],i['netmask']) for i in host_ipv4() if 'addr' in i and 'netmask' in i])
            connected_interfaces = [ i for i in host_ipv4() if 'addr' in i and 'netmask' in i and i['addr'] in local_ips ]
            logger.debug(u'Local connected IPs: %s' % [ "%s/%s" % (i['addr'],i['netmask']) for i in connected_interfaces])

            def is_inmysubnets(ip):
                """Return True if IP is in one of my connected subnets

                Returns:
                    boolean: True if ip is in one of my local connected interfaces subnets
                """
                for i in connected_interfaces:
                    if same_net(i['addr'],ip,i['netmask']):
                        logger.debug(u'  %s is in same subnet as %s/%s local connected interface' % (ip,i['addr'],i['netmask']))
                        return True
                return False

            if self.dnsdomain and self.dnsdomain != '.':
                # find by dns SRV _wapt._tcp
                try:
                    logger.debug(u'Trying _%s._tcp.%s SRV records' % (self.name,self.dnsdomain))
                    answers = windnsquery.dnsquery_srv('_%s._tcp.%s' % (self.name,self.dnsdomain))
                    if not answers:
                        logger.debug(u'  No _%s._tcp.%s SRV record found' % (self.name,self.dnsdomain))
                    else:
                        # list of (outside,priority,weight,url)
                        servers = []
                        for (priority,weight,wapthost,port) in answers:
                            # get first numerical ipv4 from SRV name record
                            try:
                                ips = windnsquery.dnsquery_a(wapthost)
                                if not ips:
                                    logger.debug('DNS Name %s is not resolvable' % wapthost)
                                else:
                                    ip = ips[0]
                                    if port == 80:
                                        url = 'http://%s/wapt' % (wapthost,)
                                        servers.append([not is_inmysubnets(ip),priority,-weight,url])
                                    elif port == 443:
                                        url = 'https://%s/wapt' % (wapthost)
                                        servers.append([not is_inmysubnets(ip),priority,-weight,url])
                                    else:
                                        url = 'http://%s:%i/wapt' % (wapthost,port)
                                        servers.append([not is_inmysubnets(ip),priority,-weight,url])
                            except Exception as e:
                                logging.debug('Unable to resolve %s : error %s' % (wapthost,ensure_unicode(e),))

                        servers.sort()
                        available_servers = []
                        for (outside,priority,weight,url) in servers:
                            probe_delay = tryurl(url+'/Packages',timeout=self.timeout,proxies=self.proxies)
                            if probe_delay is not None:
                                available_servers.append([outside,probe_delay,url])
                        if available_servers:
                            available_servers.sort()
                            return available_servers[0][2]  # [delay,url]
                        else:
                            logger.debug(u'  No wapt repo reachable with SRV request within specified timeout %s' % (self.timeout))

                except Exception as e:
                    logger.debug(u'  DNS resolver exception: %s' % (ensure_unicode(e),))
                    raise

                # find by dns CNAME
                try:
                    logger.debug(u'Trying %s.%s CNAME records' % (self.name,self.dnsdomain))
                    answers = windnsquery.dnsquery_cname('%s.%s' % (self.name,self.dnsdomain))
                    if not answers:
                        logger.debug(u'  No working %s.%s CNAME record found' % (self.name,self.dnsdomain))
                    else:
                        # list of (outside,priority,weight,url)
                        servers = []
                        available_servers = []
                        for wapthost in answers:
                            url = 'https://%s/wapt' % (wapthost,)
                            probe_delay = tryurl(url+'/Packages',timeout=self.timeout,proxies=self.proxies)
                            if probe_delay is not None:
                                available_servers.append([probe_delay,url])
                            else:
                                probe_delay = tryurl(url+'/Packages',timeout=self.timeout,proxies=self.proxies)
                                if probe_delay is not None:
                                    available_servers.append([probe_delay,url])

                        if available_servers:
                            available_servers.sort()
                            return available_servers[0][1]  # [delay,url]
                        else:
                            logger.debug(u'  No wapt repo reachable using CNAME records within specified timeout %s' % (self.timeout))


                except Exception as e:
                    logger.debug(u'  DNS error: %s' % (ensure_unicode(e),))
                    raise

                # find by dns A
                try:
                    wapthost = 'wapt.%s.' % self.dnsdomain
                    logger.debug(u'Trying %s A records' % wapthost)
                    answers = windnsquery.dnsquery_a(wapthost)
                    if answers:
                        url = 'https://%s/wapt' % (wapthost,)
                        if tryurl(url+'/Packages',timeout=self.timeout,proxies=self.proxies):
                            return url
                        url = 'http://%s/wapt' % (wapthost,)
                        if tryurl(url+'/Packages',timeout=self.timeout,proxies=self.proxies):
                            return url
                    else:
                        logger.debug(u'  No %s A record found' % wapthost)

                except Exception as e:
                    logger.debug(u'  DNS resolver exception: %s' % (ensure_unicode(e),))
                    raise

            else:
                logger.warning(u'Local DNS domain not found, skipping SRV _%s._tcp and CNAME search ' % (self.name))

            return None
        except Exception as e:
            logger.debug(u'Waptrepo.find_wapt_repo_url: exception: %s' % (e,))
            raise

    def load_config(self,config,section=None):
        """Load waptrepo configuration from inifile section.

        Use name of repo as section name if section is not provided.
        Use 'global' if no section named section in ini file
        """
        if not section:
             section = self.name

        # creates a default parser with a default section if None provided to get defaults
        if config is None:
            config = RawConfigParser(self._default_config)
            config.add_section(section)

        if not config.has_section(section):
            section = 'global'

        WaptRemoteRepo.load_config(self,config,section)
        if config.has_section(section) and config.has_option(section,'dnsdomain'):
            self.dnsdomain = config.get(section,'dnsdomain')
        return self

    def as_dict(self):
        result = super(WaptRepo,self).as_dict()
        result.update(
            {
            'repo_url':self._repo_url or self._cached_dns_repo_url,
            'dnsdomain':self.dnsdomain,
            })
        return result

    def __repr__(self):
        try:
            return '<WaptRepo %s for domain %s>' % (self.repo_url,self.dnsdomain)
        except:
            return '<WaptRepo %s for domain %s>' % ('unknown',self.dnsdomain)

class WaptHostRepo(WaptRepo):
    """Dummy http repository for host packages

    >>> host_repo = WaptHostRepo(name='wapt-host',host_id=['0D2972AC-0993-0C61-9633-529FB1A177E3','4C4C4544-004E-3510-8051-C7C04F325131'])
    >>> host_repo.load_config_from_file(r'C:\Users\htouvet\AppData\Local\waptconsole\waptconsole.ini')
    >>> host_repo.packages
    [PackageEntry('0D2972AC-0993-0C61-9633-529FB1A177E3','10') ,
     PackageEntry('4C4C4544-004E-3510-8051-C7C04F325131','30') ]
    """

    def __init__(self,url=None,name='wapt-host',verify_cert=None,http_proxy=None,timeout = None,dnsdomain=None,host_id=None,cabundle=None,config=None,host_key=None):
        self._host_id = None
        self.host_key = None
        WaptRepo.__init__(self,url=url,name=name,verify_cert=verify_cert,http_proxy=http_proxy,timeout = timeout,dnsdomain=dnsdomain,cabundle=cabundle,config=config)
        self.host_id = host_id

        if host_key:
            self.host_key = host_key

    def host_package_url(self,host_id=None):
        if host_id is None:
            if self.host_id and isinstance(self.host_id,list):
                host_id = self.host_id[0]
            else:
                host_id = self.host_id
        return  "%s/%s.wapt" % (self.repo_url,host_id)

    def is_available(self):
        logger.debug(u'Checking availability of %s' % (self.name))
        try:
            host_package_url = self.host_package_url()
            logger.debug(u'Trying to get  host package for %s at %s' % (self.host_id,host_package_url))
            host_package = requests.head(host_package_url,proxies=self.proxies,verify=self.verify_cert,timeout=self.timeout,
                headers=default_http_headers(),
                allow_redirects=True)
            host_package.raise_for_status()
            return httpdatetime2isodate(host_package.headers.get('last-modified',None))
        except requests.HTTPError as e:
            logger.info(u'No host package available at this time for %s on %s' % (self.host_id,self.name))
            return None

    def load_config(self,config,section=None):
        """Load waptrepo configuration from inifile section.

        Use name of repo as section name if section is not provided.
        Use 'global' if no section named section in ini file
        """
        if not section:
             section = self.name

        # creates a default parser with a default section if None provided to get defaults
        if config is None:
            config = RawConfigParser(self._default_config)
            config.add_section(section)

        if not config.has_section(section):
            if config.has_section('wapt-main'):
                section = 'wapt-main'
            else:
                section = 'global'

        WaptRepo.load_config(self,config,section)
        # hack to get implicit repo_url from main repo_url
        if self.repo_url and section in ['wapt-main','global'] and not self.repo_url.endswith('-host'):
            self.repo_url = self.repo_url + '-host'

        return self

    @property
    def host_id(self):
        return self._host_id

    @host_id.setter
    def host_id(self,value):
        if value != self._host_id:
            self._packages = None
            self._packages_date = None
            self._index = {}
        self._host_id = value

    def _load_packages_index(self):
        self._packages = []
        self._index = {}
        self.discarded = []
        if not self.repo_url:
            raise EWaptException('URL for WaptHostRepo repository %s is empty. Either add a wapt-host section in ini, or add a _%s._tcp.%s SRV record' % (self.name,self.name,self.dnsdomain))
        if self.host_id and not isinstance(self.host_id,list):
            host_ids = [self.host_id]
        else:
            host_ids = self.host_id

        for host_id in host_ids:
            host_package_url = self.host_package_url(host_id)
            logger.debug(u'Trying to get  host package for %s at %s' % (host_id,host_package_url))
            host_package = requests.get(host_package_url,
                proxies=self.proxies,verify=self.verify_cert,
                timeout=self.timeout,
                headers=default_http_headers(),
                allow_redirects=True)

            # prepare a package entry for further check
            package = PackageEntry()
            package.package = host_id
            package.repo = self.name
            package.repo_url = self.repo_url

            if host_package.status_code == 404:
                # host package not found
                logger.info('No host package found for %s' % host_id)
            else:
                # for other than not found error, add to the discarded list.
                # this can be consulted for mass changes to not recreate host packages because of temporary failures
                try:
                    host_package.raise_for_status()
                except requests.HTTPError as e:
                    logger.info('Discarding package for %s: error %s' % (package.package,e))
                    self.discarded.append(package)
                    continue

                content = host_package.content

                if not content.startswith(zipfile.stringFileHeader):
                    # try to decrypt package data
                    if self.host_key:
                        _host_package_content = self.host_key.decrypt_fernet(content)
                    else:
                        raise EWaptNotAPackage('Package for %s does not look like a Zip file and no key is available to try to decrypt it'% host_id)
                else:
                    _host_package_content = content

                # Packages file is a zipfile with one Packages file inside
                with ZipFile(StringIO.StringIO(_host_package_content)) as zip:
                    control_data = \
                            codecs.decode(zip.read(name='WAPT/control'),'UTF-8').splitlines()
                    package.load_control_from_wapt(control_data)
                    package.filename = package.make_package_filename()

                    try:
                        cert_data = zip.read(name='WAPT/certificate.crt')
                        signers_bundle = SSLCABundle()
                        signers_bundle.add_pem(cert_data)
                    except Exception as e:
                        logger.warning('Error reading host package certificate: %s'%repr(e))
                        signers_bundle = None

                if self.is_locally_allowed_package(package):
                    try:
                        if self.cabundle is not None:
                            package.check_control_signature(self.cabundle,signers_bundle = signers_bundle)
                        self._packages.append(package)
                        if package.package not in self._index or self._index[package.package] < package:
                            self._index[package.package] = package

                        # keep content with index as it should be small
                        package._package_content = _host_package_content
                        package._packages_date = httpdatetime2isodate(host_package.headers.get('last-modified',None))

                        # TODO better
                        self._packages_date = package._packages_date

                    except (SSLVerifyException,EWaptNotSigned) as e:
                        logger.critical("Control data of package %s on repository %s is either corrupted or doesn't match any of the expected certificates %s" % (package.asrequirement(),self.name,self.cabundle))
                        self.discarded.append(package)
                else:
                    logger.info('Discarding %s on repo "%s" because of local whitelist of blacklist rules' % (package.asrequirement(),self.name))
                    self.discarded.append(package)


    def download_packages(self,package_requests,target_dir=None,usecache=True,printhook=None):
        """Download a list of packages from repo

        Args:
            package_request (list,PackateEntry): a list of PackageEntry to download
            target_dir (str): where to store downloaded Wapt Package files
            usecache (bool): wether to try to use cached Wapt files if checksum is ok
            printhook (callable): to show progress of download

        Returns:
            dict: {"downloaded":[local filenames],"skipped":[filenames in cache],"errors":[],"packages":self.packages}
        """
        if not isinstance(package_requests,(list,tuple)):
            package_requests = [ package_requests ]
        if not target_dir:
            target_dir = tempfile.mkdtemp()
        downloaded = []
        errors = []

        self._load_packages_index()

        # if multithread... we don't have host package in memory cache from last self._load_packages_index
        for pr in package_requests:
            for pe in self.packages:
                if ((isinstance(pr,PackageEntry) and (pe == pr)) or
                   (isinstance(pr,(str,unicode)) and pe.match(pr))):
                    pfn = os.path.join(target_dir,pe.make_package_filename())
                    if pe._package_content is not None:
                        with open(pfn,'wb') as package_zip:
                            package_zip.write(pe._package_content)
                        pe.localpath = pfn
                        # for further reference
                        if isinstance(pr,PackageEntry):
                            pr.localpath = pfn
                        downloaded.append(pfn)
                        if not os.path.isfile(pfn):
                            logger.warning('Unable to write host package %s into %s' % (pr.asrequirement(),pfn))
                            errors.append(pfn)
                    else:
                        logger.warning('No host package content for %s' % (pr.asrequirement(),))
                    break

        return {"downloaded":downloaded,"skipped":[],"errors":[],"packages":self.packages}

    @property
    def repo_url(self):
        if self._repo_url:
            return self._repo_url
        else:
            if not self._cached_dns_repo_url and self.dnsdomain:
                main = self.find_wapt_repo_url()
                if main:
                    self._cached_dns_repo_url = main +'-host'
                else:
                    self._cached_dns_repo_url = None
            return self._cached_dns_repo_url

    @repo_url.setter
    def repo_url(self,value):
        if value:
            value = value.rstrip('/')

        if value != self._repo_url:
            self._repo_url = value
            self._packages = None
            self._packages_date = None
            self._cached_dns_repo_url = None

    def __repr__(self):
        try:
            return '<WaptHostRepo %s for domain %s and host_id %s >' % (self.repo_url,self.dnsdomain,self.host_id)
        except:
            return '<WaptHostRepo %s for domain %s and host id %s >' % ('unknown',self.dnsdomain,self.host_id)


######################"""

class WaptLogger(object):
    """Context handler to log all print messages to a wapt package install log"""
    def __init__(self,wapt=None,package=None):
        self.wapt = wapt
        self.package = package
        self.old_stdout = None
        self.old_stderr = None
        self.install_output = None
        self.install_id = None

        if wapt and package:
            with self.wapt.waptdb as waptdb:
                cur = waptdb.db.execute("""select rowid from wapt_localstatus where package=?""" ,(self.package,))
                pe = cur.fetchone()
                if not pe:
                    logger.critical('WaptLogger can not log info, target package %s not found in local Wapt DB install status'%package)
                else:
                    self.install_id = pe[0]

    def __enter__(self):
        if self.wapt and self.package and self.install_id is not None:
            self.old_stdout = sys.stdout
            self.old_stderr = sys.stderr
            sys.stderr = sys.stdout = self.install_output = LogInstallOutput(sys.stderr,self.wapt.waptdb,self.install_id)
        return self

    def __exit__(self, type, value, tb):
        if self.wapt and self.package and self.install_id is not None:
            sys.stdout = self.old_stdout
            sys.stderr = self.old_stderr
            if not tb:
                self.wapt.waptdb.update_install_status(self.install_id,'OK','')
            else:
                self.wapt.waptdb.update_install_status(self.install_id,'ERROR',traceback.format_exc())
            self.wapt.update_server_status()
            self.install_id = None
            self.install_output = None
            self.wapt = None

class Wapt(BaseObjectClass):
    """Global WAPT engine"""
    global_attributes = ['wapt_base_dir','waptserver','config_filename','proxies','repositories','personal_certificate_path','public_certs_dir','package_cache_dir','dbpath']

    def __init__(self,config=None,config_filename=None,defaults=None,disable_update_server_status=True):
        """Initialize engine with a configParser instance (inifile) and other defaults in a dictionary
        >>> wapt = Wapt(config_filename='c:/wapt/wapt-get.ini')
        >>> updates = wapt.update()
        >>> 'count' in updates and 'added' in updates and 'upgrades' in updates and 'date' in updates and 'removed' in updates
        True
        """
        # used to signal to cancel current operations ASAP
        self.task_is_cancelled = threading.Event()

        assert not config or isinstance(config,RawConfigParser)
        self._waptdb = None
        self._waptsessiondb = None
        self._dbpath = None
        # cached runstatus to avoid setting in db if not changed.
        self._runstatus = None
        self._use_hostpackages = None

        self.repositories = []

        self.dry_run = False

        self.upload_cmd = None
        self.upload_cmd_host = self.upload_cmd
        self.after_upload = None
        self.proxies = None
        self.language = setuphelpers.get_language()
        self.locales = [setuphelpers.get_language()]
        self.maturities = ['PROD']

        self.use_http_proxy_for_repo = False
        self.use_http_proxy_for_server = False
        self.use_http_proxy_for_templates = False

        self.forced_uuid = None
        self.use_fqdn_as_uuid = False

        self.forced_host_dn = None

        try:
            self.wapt_base_dir = os.path.dirname(__file__)
        except NameError:
            self.wapt_base_dir = os.getcwdu()

        self.disable_update_server_status = disable_update_server_status

        self.config = config
        self.config_filename = config_filename
        if not self.config_filename:
            self.config_filename = os.path.join(self.wapt_base_dir,'wapt-get.ini')

        self.package_cache_dir = os.path.join(os.path.dirname(self.config_filename),'cache')
        if not os.path.exists(self.package_cache_dir):
            os.makedirs(self.package_cache_dir)

        # to allow/restrict installation, supplied to packages
        self.user = setuphelpers.get_current_user()
        self.usergroups = None

        self.sign_digests = ['sha256']

        # host key cache
        self._host_key = None

        #self.key_passwd_callback = None

        # keep private key in cache
        self._private_key_cache = None

        self.cabundle = SSLCABundle()
        self.check_certificates_validity = False

        self.waptserver = None
        self.config_filedate = None

        self.packages_whitelist = None
        self.packages_blacklist = None

        self.load_config(config_filename = self.config_filename)

        self.options = OptionParser()
        self.options.force = False

        # list of process pids launched by run command
        self.pidlist = []

        # events handler
        self.events = None

        self.progress_hook = None

        import pythoncom
        pythoncom.CoInitialize()

    def __enter__(self):
        return self

    def __exit__(self, type, value, tb):
        pass

    def as_dict(self):
        result = {}
        for att in self.global_attributes:
            result[att] = getattr(self,att)
        return result

    @property
    def dbdir(self):
        if self._waptdb:
            if self._dbpath != ':memory:':
                return os.path.dirname(self._dbpath)
            else:
                return None
        else:
            return None

    @property
    def dbpath(self):
        if self._waptdb:
            return self._waptdb.dbpath
        elif self._dbpath:
            return self._dbpath
        else:
            return None

    @dbpath.setter
    def dbpath(self,value):
        # check if not changed
        if self._waptdb and self._waptdb.dbpath == value:
            exit
        # updated : reset db
        self._waptdb = None
        self._dbpath = value

    @property
    def use_hostpackages(self):
        return self._use_hostpackages

    @use_hostpackages.setter
    def use_hostpackages(self,value):
        if value and not self._use_hostpackages == True:
            self.add_hosts_repo()
        elif not value and self._use_hostpackages:
            if self.repositories and isinstance(self.repositories[-1],WaptHostRepo):
                del self.repositories[-1]
        self._use_hostpackages = value

    def load_config(self,config_filename=None):
        """Load configuration parameters from supplied inifilename
        """
        # default config file
        defaults = {
            'loglevel':'warning',
            'log_to_windows_events':'0',
            'default_package_prefix':'tis',
            'default_sources_suffix':'wapt',
            'default_sources_root':'c:\\waptdev',
            'use_http_proxy_for_repo':'0',
            'use_http_proxy_for_server':'0',
            'use_http_proxy_for_templates':'0',
            'tray_check_interval':2,
            'service_interval':2,
            'use_hostpackages':'0',
            'timeout':5.0,
            'wapt_server_timeout':10.0,
            # optional...
            'templates_repo_url':'',
            'default_sources_url':'',
            'upload_cmd':'',
            'upload_cmd_host':'',
            'after_upload':'',
            'http_proxy':'',
            'waptwua_enabled':'0',
            'personal_certificate_path':'',
            'check_certificates_validity':'True',
            'sign_digests':'sha256',
            }

        if not self.config:
            self.config = RawConfigParser(defaults = defaults)

        if config_filename:
            self.config_filename = config_filename

        self.config.read(self.config_filename)
        # keep the timestamp of last read config file to reload it if it is changed
        if os.path.isfile(self.config_filename):
            self.config_filedate = os.stat(self.config_filename).st_mtime
        else:
            self.config_filedate = None

        if self.config.has_option('global','dbpath'):
            self.dbpath =  self.config.get('global','dbpath').decode('utf8')
        else:
            self.dbpath = os.path.join(self.wapt_base_dir,'db','waptdb.sqlite')

        # must have a matching key eithe rin same file or in same directory
        # see self.private_key()
        if self.config.has_option('global','personal_certificate_path'):
            self.personal_certificate_path = self.config.get('global','personal_certificate_path').decode('utf8')

        # be smart with old config
        if not self.personal_certificate_path and self.config.has_option('global','private_key'):
            pk = self.config.get('global','private_key').decode('utf8')
            if pk and os.path.isfile(pk):
                (root,ext) = os.path.splitext(pk)
                if os.path.isfile(root+'.crt'):
                    self.personal_certificate_path = root+'.crt'

        if self.config.has_option('global','public_certs_dir'):
            self.public_certs_dir = self.config.get('global','public_certs_dir').decode('utf8')
        else:
            self.public_certs_dir = os.path.join(self.wapt_base_dir,'ssl')

        self.cabundle.clear()
        self.cabundle.add_pems(self.public_certs_dir)

        if self.config.has_option('global','check_certificates_validity'):
            self.check_certificates_validity = self.config.getboolean('global','check_certificates_validity')

        if self.config.has_option('global','upload_cmd'):
            self.upload_cmd = self.config.get('global','upload_cmd')

        if self.config.has_option('global','upload_cmd_host'):
            self.upload_cmd_host = self.config.get('global','upload_cmd_host')

        if self.config.has_option('global','after_upload'):
            self.after_upload = self.config.get('global','after_upload')

        self.use_http_proxy_for_repo = self.config.getboolean('global','use_http_proxy_for_repo')
        self.use_http_proxy_for_server = self.config.getboolean('global','use_http_proxy_for_server')
        self.use_http_proxy_for_templates = self.config.getboolean('global','use_http_proxy_for_templates')

        if self.config.has_option('global','http_proxy'):
            self.proxies = {'http':self.config.get('global','http_proxy'),'https':self.config.get('global','http_proxy')}
        else:
            self.proxies = None

        if self.config.has_option('global','wapt_server'):
            self.waptserver = WaptServer().load_config(self.config)
        else:
            # force reset to None if config file is changed at runtime
            self.waptserver = None

        if self.config.has_option('global','language'):
            self.language = self.config.get('global','language')

        if self.config.has_option('global','uuid'):
            self.forced_uuid = self.config.get('global','uuid')
            if self.forced_uuid != self.host_uuid:
                logger.debug('Storing new uuid in DB %s' % self.forced_uuid)
                self.host_uuid = self.forced_uuid
        else:
            # force reset to None if config file is changed at runtime
            self.forced_uuid = None

        if self.config.has_option('global','use_fqdn_as_uuid'):
            self.use_fqdn_as_uuid = self.config.getboolean('global','use_fqdn_as_uuid')

        if self.config.has_option('global','sign_digests'):
            self.sign_digests = ensure_list(self.config.get('global','sign_digests'))

        # allow to force a host_dn when the computer is not part of an AD, but we want to put host in a OR.
        if self.config.has_option('global','host_dn'):
            self.forced_host_dn = self.config.get('global','host_dn')
            if self.forced_host_dn != self.host_dn:
                logger.debug('Storing new forced hos_dn DB %s' % self.forced_host_dn)
                self.host_dn = self.forced_host_dn
        else:
            # force reset to None if config file is changed at runtime
            self.forced_host_dn = None

        if self.config.has_option('global','packages_whitelist'):
            self.packages_whitelist = ensure_list(self.config.get('global','packages_whitelist'),allow_none=True)

        if self.config.has_option('global','packages_blacklist'):
            self.packages_blacklist = ensure_list(self.config.get('global','packages_blacklist'),allow_none=True)

        if self.config.has_option('global','locales'):
            self.locales = ensure_list(self.config.get('global','locales'),allow_none=True)

        if self.config.has_option('global','maturities'):
            self.maturities = ensure_list(self.config.get('global','maturities'),allow_none=True)

        # Get the configuration of all repositories (url, ...)
        self.repositories = []
        # secondary
        if self.config.has_option('global','repositories'):
            repository_names = ensure_list(self.config.get('global','repositories'))
            logger.info(u'Other repositories : %s' % (repository_names,))
            for name in repository_names:
                if name:
                    w = WaptRepo(name=name).load_config(self.config,section=name)
                    if w.cabundle is None:
                        w.cabundle = self.cabundle
                    self.repositories.append(w)
                    logger.debug(u'    %s:%s' % (w.name,w._repo_url))
        else:
            repository_names = []

        # last is main repository so it overrides the secondary repositories
        if self.config.has_option('global','repo_url') and not 'wapt' in repository_names:
            w = WaptRepo(name='wapt').load_config(self.config)
            self.repositories.append(w)
            if w.cabundle is None:
                w.cabundle = self.cabundle

        # True if we want to use automatic host package based on host fqdn
        #   privacy problem as there is a request to wapt repo to get
        #   host package update at each update/upgrade
        self._use_hostpackages = None
        if self.config.has_option('global','use_hostpackages'):
            self.use_hostpackages = self.config.getboolean('global','use_hostpackages')

        self.waptwua_enabled = False
        if self.config.has_option('global','waptwua_enabled'):
            self.waptwua_enabled = self.config.getboolean('global','waptwua_enabled')

        # clear host key cache
        self._host_key = None

        return self

    def write_config(self,config_filename=None):
        """Update configuration parameters to supplied inifilename
        """
        for key in self.config.defaults():
            if hasattr(self,key) and getattr(self,key) != self.config.defaults()[key]:
                logger.debug('update config global.%s : %s' % (key,getattr(self,key)))
                self.config.set('global',key,getattr(self,key))
        repositories_names = ','.join([ r.name for r in self.repositories if r.name not in ('global','wapt-host')])
        if self.config.has_option('global','repositories') and repositories_names != '':
            self.config.set('global','repositories',repositories_names)
        self.config.write(open(self.config_filename,'wb'))
        self.config_filedate = os.stat(self.config_filename).st_mtime

    def _set_fake_hostname(self,fqdn):
        setuphelpers._fake_hostname = fqdn
        logger.warning('Using test fake hostname and uuid: %s'%fqdn)
        self.use_fqdn_as_uuid = fqdn
        logger.debug('Host uuid is now: %s'%self.host_uuid)
        logger.debug('Host computer_name is now: %s'%setuphelpers.get_computername())

    def add_hosts_repo(self):
        """Add an automatic host repository, remove existing WaptHostRepo last one before"""
        while self.repositories and isinstance(self.repositories[-1],WaptHostRepo):
            del self.repositories[-1]

        main = None
        if self.repositories:
            main = self.repositories[-1]

        if self.config.has_section('wapt-host'):
            section = 'wapt-host'
        else:
            section = None

        try:
            host_key = self.get_host_key()
        except Exception as e:
            # unable to access or create host key
            host_key = None

        host_repo = WaptHostRepo(name='wapt-host',config=self.config,host_id=self.host_packagename(),host_key=host_key)
        self.repositories.append(host_repo)
        if host_repo.cabundle is None:
            host_repo.cabundle = self.cabundle

        # in case host repo is guessed from main repo (no specific section) ans main repor_url is set
        if section is None and main and main._repo_url:
            host_repo.repo_url = main._repo_url+'-host'


    def reload_config_if_updated(self):
        """Check if config file has been updated,
        Return None if config has not changed or date of new config file if reloaded

        >>> wapt = Wapt(config_filename='c:/wapt/wapt-get.ini')
        >>> wapt.reload_config_if_updated()

        """
        if os.path.exists(self.config_filename):
            new_config_filedate = os.stat(self.config_filename).st_mtime
            if new_config_filedate!=self.config_filedate:
                self.load_config()
                return new_config_filedate
            else:
                return None
        else:
            return None

    @property
    def waptdb(self):
        """Wapt database"""
        if not self._waptdb:
            self._waptdb = WaptDB(dbpath=self.dbpath)
            if self._waptdb.db_version < self._waptdb.curr_db_version:
                logger.info(u'Upgrading db structure from %s to %s' % (self._waptdb.db_version,self._waptdb.curr_db_version))
                self._waptdb.upgradedb()
        return self._waptdb

    @property
    def waptsessiondb(self):
        """Wapt user session database"""
        if not self._waptsessiondb:
            self._waptsessiondb = WaptSessionDB(username=setuphelpers.get_current_user())
            if self._waptsessiondb.db_version < self._waptsessiondb.curr_db_version:
                logger.info(u'Upgrading db structure from %s to %s' % (self._waptsessiondb.db_version,self._waptsessiondb.curr_db_version))
                self._waptsessiondb.upgradedb()
        return self._waptsessiondb

    @property
    def runstatus(self):
        """returns the current run status for tray display"""
        return self.read_param('runstatus','')

    @runstatus.setter
    def runstatus(self,waptstatus):
        """Stores in local db the current run status for tray display"""
        if self._runstatus is None or self._runstatus != waptstatus:
            logger.info(u'Status : %s' % ensure_unicode(waptstatus))
            self.write_param('runstatus',waptstatus)
            self._runstatus = waptstatus
            if not self.disable_update_server_status and self.waptserver_available():
                try:
                    self.update_server_status()
                except Exception as e:
                    logger.warning(u'Unable to contact server to register current status')
                    logger.debug(u'Unable to update server with current status : %s' % ensure_unicode(e))

    @property
    def host_uuid(self):
        previous_uuid = self.read_param('uuid')
        new_uuid = None

        registered_hostname = self.read_param('hostname')
        current_hostname = setuphelpers.get_hostname()

        if self.forced_uuid:
            new_uuid = self.forced_uuid
        elif self.use_fqdn_as_uuid:
            new_uuid = current_hostname
        else:
            new_uuid = previous_uuid

        if not previous_uuid or previous_uuid != new_uuid or registered_hostname != current_hostname:
            if previous_uuid != new_uuid:
                # forget old host package if any as it is not relevant anymore
                self.forget_packages(previous_uuid)
            logger.info('Unknown UUID or hostname has changed: reading host UUID')
            if new_uuid is None:
                logger.info('reading custom host UUID from WMI System Information.')
                try:
                    inv = setuphelpers.wmi_info_basic()
                    new_uuid = inv['System_Information']['UUID']
                except:
                    # random uuid if wmi is not working
                    new_uuid = str(uuid.uuid4())
            self.write_param('uuid',new_uuid)
            self.write_param('hostname',current_hostname)
        return new_uuid


    @host_uuid.setter
    def host_uuid(self,value):
        self.forced_uuid = value
        self.write_param('uuid',value)


    @host_uuid.deleter
    def host_uuid(self):
        self.forced_uuid = None
        self.delete_param('uuid')

    def generate_host_uuid(self,forced_uuid=None):
        """Regenerate a random UUID for this host or force with supplied one.

        Normally, the UUID is taken from BIOS through wmi.

        In case bios returns some duplicates or garbage, it can be useful to
        force a random uuid. This is stored as uuid key in wapt-get.ini.

        In case we want to link th host with a an existing record on server, we
        can force a old UUID.

        Args;
            forced_uuid (str): uuid to force for this host. If None, generate a random one

        """
        auuid = forced_uuid or str(uuid.uuid4())
        self.host_uuid = auuid
        ini = RawConfigParser()
        ini.read(self.config_filename)
        ini.set('global','uuid',auuid)
        ini.write(open(self.config_filename,'w'))
        return auuid

    def reset_host_uuid(self):
        """Reset host uuid to bios provided UUID.
        If it was forced in ini file, remove setting from ini file.
        """
        del(self.host_uuid)
        ini = RawConfigParser()
        ini.read(self.config_filename)
        if ini.has_option('global','uuid'):
            ini.remove_option('global','uuid')
            ini.write(open(self.config_filename,'w'))
        return self.host_uuid


    @property
    def host_dn(self):
        """Get host DN from wapt-get.ini [global] host_dn if defined
        or from registry as supplied by AD / GPO process
        """

        previous_host_dn = self.read_param('host_dn')
        default_host_dn = setuphelpers.registry_readstring(HKEY_LOCAL_MACHINE,r'SOFTWARE\Microsoft\Windows\CurrentVersion\Group Policy\State\Machine','Distinguished-Name')
        new_host_dn = None

        if self.forced_host_dn:
            new_host_dn = self.forced_host_dn
        elif previous_host_dn:
            new_host_dn = previous_host_dn
        else:
            new_host_dn = default_host_dn

        if not previous_host_dn or previous_host_dn != new_host_dn:
            self.write_param('host_dn',new_host_dn)
        return new_host_dn

    @host_dn.setter
    def host_dn(self,value):
        self.forced_host_dn = value
        self.write_param('host_dn',value)

    @host_dn.deleter
    def host_dn(self):
        self.forced_host_dn = None
        self.delete_param('host_dn')

    def reset_host_dn(self):
        """Reset forced host to AD / GPO registry defaults.
        If it was forced in ini file, remove setting from ini file.
        """
        del(self.host_dn)
        ini = RawConfigParser()
        ini.read(self.config_filename)
        if ini.has_option('global','host_dn'):
            ini.remove_option('global','host_dn')
            ini.write(open(self.config_filename,'w'))
        return self.host_dn


    def http_upload_package(self,packages,wapt_server_user=None,wapt_server_passwd=None):
        r"""Upload a package or host package to the waptserver.

        Args:
            packages (str or list): list of filepaths or PackageEntry to wapt packages to upload
            wapt_server_user (str)   : user for basic auth on waptserver
            wapt_server_passwd (str) : password for basic auth on waptserver

        Returns:


        >>> from common import *
        >>> wapt = Wapt(config_filename = r'C:\tranquilit\wapt\tests\wapt-get.ini')
        >>> r = wapt.update()
        >>> d = wapt.duplicate_package('tis-wapttest','toto')
        >>> print d
        {'target': u'c:\\users\\htouvet\\appdata\\local\\temp\\toto.wapt', 'package': PackageEntry('toto','119')}
        >>> wapt.http_upload_package(d['package'],wapt_server_user='admin',wapt_server_passwd='password')
        """
        packages = ensure_list(packages)

        # force auth before trying to upload to avoid uncessary upload buffering server side before it send a 401.
        auth = None
        if wapt_server_user:
            auth = (wapt_server_user, wapt_server_passwd)
        else:
            auth = self.waptserver.ask_user_password('%s/%s' % (self.waptserver.server_url,'api/v3/upload_xxx'))

        files = {}
        is_hosts = None

        for package in packages:
            if not isinstance(package,PackageEntry):
                pe = PackageEntry(waptfile = package)
                package_filename = package
            else:
                pe = package
                package_filename = pe.localpath

            if is_hosts is None and pe.section == 'host':
                is_hosts = True

            if is_hosts:
                # small files
                with open(package_filename,'rb') as f:
                    files[os.path.basename(package_filename)] = f.read()
            else:
                # stream
                #files[os.path.basename(package_filename)] = open(package_filename,'rb')
                files[os.path.basename(package_filename)] = FileChunks(package_filename)

        if files:
            try:
                if is_hosts:
                    logger.info('Uploading %s host packages' % len(files))
                    # single shot
                    res = self.waptserver.post('api/v3/upload_hosts',files=files,auth=auth,timeout=300)
                    if not res['success']:
                        raise Exception('Error when uploading host packages: %s'% (res['msg']))
                else:
                    ok = []
                    errors = []
                    for (fn,f) in files.iteritems():
                        res_partiel = self.waptserver.post('api/v3/upload_packages',data=f.get(),auth=auth,timeout=300)
                        if not res_partiel['success']:
                            errors.append(res_partiel)
                        else:
                            ok.append(res_partiel)
                    res = {'success':len(errors)==0,'result':{'ok':ok,'errors':errors},'msg':'%s Packages uploaded, %s errors' % (len(ok),len(errors))}
            finally:
                for f in files.values():
                    if isinstance(f,file):
                        f.close()
            return res
        else:
            raise Exception('No package to upload')

    def upload_package(self,filenames,wapt_server_user=None,wapt_server_passwd=None):
        """Method to upload a package using Shell command (like scp) instead of http upload
            You must define first a command in inifile with the form :
                upload_cmd="c:\Program Files"\putty\pscp -v -l waptserver %(waptfile)s srvwapt:/var/www/%(waptdir)s/
            or
                upload_cmd="C:\Program Files\WinSCP\WinSCP.exe" root@wapt.tranquilit.local /upload %(waptfile)s
            You can define a "after_upload" shell command. Typical use is to update the Packages index
                after_upload="c:\Program Files"\putty\plink -v -l waptserver srvwapt.tranquilit.local "python /opt/wapt/wapt-scanpackages.py /var/www/%(waptdir)s/"
        """
        if self.upload_cmd:
            args = dict(filenames = " ".join('"%s"' % fn for fn in filenames),)
            return dict(status='OK',message=ensure_unicode(self.run(self.upload_cmd % args )))
        else:
            return self.http_upload_package(filenames,wapt_server_user=wapt_server_user,wapt_server_passwd=wapt_server_passwd)

    def check_install_running(self,max_ttl=60):
        """ Check if an install is in progress, return list of pids of install in progress
            Kill old stucked wapt-get processes/children and update db status
            max_ttl is maximum age of wapt-get in minutes
        """

        logger.debug(u'Checking if old install in progress')
        # kill old wapt-get
        mindate = time.time() - max_ttl*60

        killed=[]
        for p in psutil.process_iter():
            try:
                if p.pid != os.getpid() and (p.create_time() < mindate) and p.name() in ('wapt-get','wapt-get.exe'):
                    logger.debug(u'Killing process tree of pid %i' % p.pid)
                    setuphelpers.killtree(p.pid)
                    logger.debug(u'Killing pid %i' % p.pid)
                    killed.append(p.pid)
            except (psutil.NoSuchProcess,psutil.AccessDenied):
                pass

        # reset install_status
        with self.waptdb:
            logger.debug(u'reset stalled install_status in database')
            init_run_pids = self.waptdb.query("""\
               select process_id from wapt_localstatus
                  where install_status in ('INIT','RUNNING')
               """ )

            all_pids = psutil.pids()
            reset_error = []
            result = []
            for rec in init_run_pids:
                # check if process is no more running
                if not rec['process_id'] in all_pids or rec['process_id'] in killed:
                    reset_error.append(rec['process_id'])
                else:
                    # install in progress
                    result.append(rec['process_id'])

            for pid in reset_error:
                self.waptdb.update_install_status_pid(pid,'ERROR')

            if reset_error or not init_run_pids:
                self.runstatus = ''

        # return pids of install in progress
        return result


    @property
    def pre_shutdown_timeout(self):
        """get / set the pre shutdown timeout shutdown tasks.
        """
        if setuphelpers.reg_key_exists(HKEY_LOCAL_MACHINE,r'SYSTEM\CurrentControlSet\services\gpsvc'):
            with setuphelpers.reg_openkey_noredir(HKEY_LOCAL_MACHINE,r'SYSTEM\CurrentControlSet\services\gpsvc') as key:
                ms = setuphelpers.reg_getvalue(key,'PreshutdownTimeout',None)
                if ms:
                    return ms / (60*1000)
                else:
                    return None
        else:
            return None

    @pre_shutdown_timeout.setter
    def pre_shutdown_timeout(self,minutes):
        """Set PreshutdownTimeout"""
        if setuphelpers.reg_key_exists(HKEY_LOCAL_MACHINE,r'SYSTEM\CurrentControlSet\services\gpsvc'):
            key = setuphelpers.reg_openkey_noredir(HKEY_LOCAL_MACHINE,r'SYSTEM\CurrentControlSet\services\gpsvc',sam=setuphelpers.KEY_WRITE)
            if not key:
                raise Exception('The PreshutdownTimeout can only be changed with System Account rights')
            setuphelpers.reg_setvalue(key,'PreshutdownTimeout',minutes*60*1000,setuphelpers.REG_DWORD)

    @property
    def max_gpo_script_wait(self):
        """get / set the MaxGPOScriptWait.
        """
        with setuphelpers.reg_openkey_noredir(HKEY_LOCAL_MACHINE,r'SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System') as key:
            ms = setuphelpers.reg_getvalue(key,'MaxGPOScriptWait',None)
            if ms:
                return ms / (60*1000)
            else:
                return None

    @max_gpo_script_wait.setter
    def max_gpo_script_wait(self,minutes):
        """Set MaxGPOScriptWait"""
        key = setuphelpers.reg_openkey_noredir(HKEY_LOCAL_MACHINE,r'SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System',sam=setuphelpers.KEY_WRITE)
        if not key:
            raise Exception('The MaxGPOScriptWait can only be changed with System Account rights')
        setuphelpers.reg_setvalue(key,'MaxGPOScriptWait',minutes*60*1000,setuphelpers.REG_DWORD)


    @property
    def hiberboot_enabled(self):
        """get HiberbootEnabled.
        """
        key = setuphelpers.reg_openkey_noredir(HKEY_LOCAL_MACHINE, r'SYSTEM\CurrentControlSet\Control\Session Manager\Power')
        return key and setuphelpers.reg_getvalue(key,'HiberbootEnabled',None)

    @hiberboot_enabled.setter
    def hiberboot_enabled(self,enabled):
        """Set HiberbootEnabled (0/1)"""
        key = setuphelpers.reg_openkey_noredir(HKEY_LOCAL_MACHINE,r'SYSTEM\CurrentControlSet\Control\Session Manager\Power',sam=setuphelpers.KEY_WRITE)
        if key:
            setuphelpers.reg_setvalue(key,'HiberbootEnabled',enabled,setuphelpers.REG_DWORD)


    def registry_uninstall_snapshot(self):
        """Return list of uninstall ID from registry
             launched before and after an installation to capture uninstallkey
        """
        result = []
        with setuphelpers.reg_openkey_noredir(HKEY_LOCAL_MACHINE,"Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall") as key:
            try:
                i = 0
                while True:
                    subkey = EnumKey(key, i)
                    result.append(subkey)
                    i += 1
            except WindowsError as e:
                # WindowsError: [Errno 259] No more data is available
                if e.winerror == 259:
                    pass
                else:
                    raise

        if platform.machine() == 'AMD64':
            with setuphelpers.reg_openkey_noredir(HKEY_LOCAL_MACHINE,"Software\\Wow6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall") as key:
                try:
                    i = 0
                    while True:
                        subkey = EnumKey(key, i)
                        result.append(subkey)
                        i += 1
                except WindowsError as e:
                    # WindowsError: [Errno 259] No more data is available
                    if e.winerror == 259:
                        pass
                    else:
                        raise
        return result

    def uninstall_cmd(self,guid):
        """return the (quiet) command stored in registry to uninstall a software given its registry key"""
        return setuphelpers.uninstall_cmd(guid)

    def set_local_password(self,user='admin',pwd='password'):
        """Set admin/password local auth for waptservice in ini file as a sha256 hex hash"""
        conf = RawConfigParser()
        conf.read(self.config_filename)
        conf.set('global','waptservice_user',user)
        conf.set('global','waptservice_password',hashlib.sha256(pwd).hexdigest())
        conf.write(open(self.config_filename,'wb'))

    def reset_local_password(self):
        """Remove the local waptservice auth from ini file"""
        conf = RawConfigParser()
        conf.read(self.config_filename)
        if conf.has_option('global','waptservice_user'):
            conf.remove_option('global','waptservice_user')
        if conf.has_option('global','waptservice_password'):
            conf.remove_option('global','waptservice_password')
        conf.write(open(self.config_filename,'wb'))

    def check_cancelled(self,msg='Task cancelled'):
        if self.task_is_cancelled.is_set():
            raise EWaptCancelled(msg)

    def run(self,*arg,**args):
        return ensure_unicode(setuphelpers.run(*arg,pidlist=self.pidlist,**args))

    def run_notfatal(self,*cmd,**args):
        """Runs the command and wait for it termination
        returns output, don't raise exception if exitcode is not null but return '' """
        try:
            return self.run(*cmd,**args)
        except Exception as e:
            print('Warning : %s' % repr(e))
            return ''


    def install_wapt(self,fname,params_dict={},explicit_by=None):
        """Install a single wapt package given its WAPT filename.
        return install status

        Args:
            fname (str): Path to wapt Zip file or unzipped development directory
            params (dict): custom parmaters for the install function
            explicit_by (str): identify who has initiated the install

        Returns:
            str:  'OK','ERROR'

        Raises:

            EWaptMissingCertificate
            EWaptNeedsNewerAgent
            EWaptUnavailablePackage
            EWaptConflictingPackage
            EWaptBadTargetOS
            EWaptException
            various Exception depending on setup script
        """
        install_id = None
        old_hdlr = None
        old_stdout = sys.stdout
        old_stderr = sys.stderr

        # we  record old sys.path as we will include current setup.py
        oldpath = sys.path

        self.check_cancelled(u'Install of %s cancelled before starting up'%ensure_unicode(fname))
        logger.info(u"Register start of install %s as user %s to local DB with params %s" % (ensure_unicode(fname), setuphelpers.get_current_user(), params_dict))
        logger.info(u"Interactive user:%s, usergroups %s" % (self.user,self.usergroups))


        previous_uninstall = self.registry_uninstall_snapshot()

        try:

            status = 'INIT'
            if not self.cabundle:
                raise EWaptMissingCertificate(u'install_wapt %s: No public Key provided for package signature checking.'%(fname,))

            entry = PackageEntry(waptfile=fname)
            self.runstatus=u"Installing package %s version %s ..." % (entry.package,entry.version)

            # get old install params if the package has been already installed
            old_install = self.is_installed(entry.package)
            if old_install:
                old_install_params = json.loads(old_install['install_params'])
                for name in old_install_params:
                    if not name in params_dict:
                        params_dict[name] = old_install_params[name]

            install_id = self.waptdb.add_start_install(
                package=entry.package ,
                version=entry.version,
                architecture=entry.architecture,
                params_dict=params_dict,explicit_by=explicit_by,
                maturity=entry.maturity,
                locale=entry.locale,
                depends=entry.depends,
                conflicts=entry.conflicts,
                impacted_process=entry.impacted_process,
                )

            if entry.min_wapt_version and Version(entry.min_wapt_version)>Version(setuphelpers.__version__):
                raise EWaptNeedsNewerAgent('This package requires a newer Wapt agent. Minimum version: %s' % entry.min_wapt_version)

            depends = ensure_list(entry.depends)
            conflicts = ensure_list(entry.conflicts)

            missing_depends = [ p for p in depends if not self.is_installed(p)]
            installed_conflicts = [ p for p in conflicts if self.is_installed(p)]

            if missing_depends:
                raise EWaptUnavailablePackage('Missing dependencies: %s' % (','.join(missing_depends,)))

            if installed_conflicts:
                raise EWaptConflictingPackage('Conflicting packages installed: %s' % (','.join(installed_conflicts,)))

            # check if there is enough space for final install
            # TODO : space for the temporary unzip ?
            free_disk_space = setuphelpers.get_disk_free_space(setuphelpers.programfiles)
            if entry.installed_size and free_disk_space < entry.installed_size:
                raise EWaptDiskSpace('This package requires at least %s free space. The "Program File"s drive has only %s free space' %
                    (format_bytes(entry.installed_size),format_bytes(free_disk_space)))

            if entry.target_os and entry.target_os != 'windows':
                raise EWaptBadTargetOS('This package is designed for OS %s' % entry.target_os)
            os_version = setuphelpers.windows_version()
            if entry.min_os_version and os_version < Version(entry.min_os_version):
                raise EWaptBadTargetOS('This package requires that OS be at least %s' % entry.min_os_version)
            if entry.max_os_version and os_version > Version(entry.max_os_version):
                raise EWaptBadTargetOS('This package requires that OS be at most %s' % entry.min_os_version)

            # don't check in developper mode
            if os.path.isfile(fname):
                cert = entry.check_control_signature(self.cabundle)
                logger.info(u'Control data for package %s verified by certificate %s' % (setuphelpers.ensure_unicode(fname),cert))
            else:
                logger.info(u'Developper mode, don''t check control signature for %s' % setuphelpers.ensure_unicode(fname))

            # we setup a redirection of stdout to catch print output from install scripts
            sys.stderr = sys.stdout = install_output = LogInstallOutput(sys.stderr,self.waptdb,install_id)

            self.check_cancelled()
            logger.info(u"Installing package %s"%(ensure_unicode(fname),))
            # case where fname is a wapt zipped file, else directory (during developement)
            istemporary = False

            if os.path.isfile(fname):
                # check signature and files when unzipping
                packagetempdir = entry.unzip_package(cabundle=self.cabundle)
                istemporary = True
            elif os.path.isdir(fname):
                packagetempdir = fname
            else:
                raise EWaptNotAPackage(u'%s is not a file nor a directory, aborting.' % ensure_unicode(fname))

            try:
                previous_cwd = os.getcwdu()
                self.check_cancelled()

                exitstatus = None
                new_uninstall_key = None
                uninstallstring = None

                setup_filename = os.path.join( packagetempdir,'setup.py')

                # take in account the case we have no setup.py
                if os.path.isfile(setup_filename):
                    os.chdir(os.path.dirname(setup_filename))
                    if not os.getcwdu() in sys.path:
                        sys.path.append(os.getcwdu())

                    # import the setup module from package file
                    logger.info(u"  sourcing install file %s " % ensure_unicode(setup_filename) )
                    setup = import_setup(setup_filename)
                    required_params = []

                    # be sure some minimal functions are available in setup module at install step
                    setattr(setup,'basedir',os.path.dirname(setup_filename))
                    # redefine run to add reference to wapt.pidlist
                    setattr(setup,'run',self.run)
                    setattr(setup,'run_notfatal',self.run_notfatal)

                    # to set some contextual default arguments
                    def with_install_context(func,impacted_process=None,uninstallkeylist=None,force=None,pidlist=None):
                        def new_func(*args,**kwargs):
                            if impacted_process and not 'killbefore' in kwargs:
                                kwargs['killbefore'] = impacted_process
                            if uninstallkeylist is not None and not 'uninstallkeylist' in kwargs:
                                kwargs['uninstallkeylist'] = uninstallkeylist
                            if force is not None and not 'force' in kwargs:
                                kwargs['force'] = force
                            if pidlist is not None and not 'pidlist' in kwargs:
                                kwargs['pidlist'] = pidlist
                            return func(*args,**kwargs)
                        return new_func

                    setattr(setup,'install_msi_if_needed',with_install_context(setuphelpers.install_msi_if_needed,entry.impacted_process,setup.uninstallkey,self.options.force,self.pidlist))
                    setattr(setup,'install_exe_if_needed',with_install_context(setuphelpers.install_exe_if_needed,entry.impacted_process,setup.uninstallkey,self.options.force,self.pidlist))
                    setattr(setup,'WAPT',self)
                    setattr(setup,'control',entry)
                    setattr(setup,'language',self.language)

                    setattr(setup,'user',self.user)
                    setattr(setup,'usergroups',self.usergroups)

                    # get definitions of required parameters from setup module
                    if hasattr(setup,'required_params'):
                        required_params = setup.required_params

                    # get value of required parameters if not already supplied
                    for p in required_params:
                        if not p in params_dict:
                            if not is_system_user():
                                params_dict[p] = raw_input(u"%s: " % p)
                            else:
                                raise EWaptException(u'Required parameters %s is not supplied' % p)
                    logger.info(u'Install parameters : %s' % (params_dict,))

                    # set params dictionary
                    if not hasattr(setup,'params'):
                        # create a params variable for the setup module
                        setattr(setup,'params',params_dict)
                    else:
                        # update the already created params with additional params from command line
                        setup.params.update(params_dict)

                    # store source of install and params in DB for future use (upgrade, session_setup, uninstall)
                    self.waptdb.store_setuppy(install_id, setuppy = codecs.open(setup_filename,'r',encoding='utf-8').read(),install_params=params_dict)

                    if not self.dry_run:
                        try:
                            logger.info(u"  executing install script")
                            exitstatus = setup.install()
                        except Exception as e:
                            logger.critical(u'Fatal error in install script: %s:\n%s' % (ensure_unicode(e),ensure_unicode(traceback.format_exc())))
                            raise
                    else:
                        logger.warning(u'Dry run, not actually running setup.install()')
                        exitstatus = None

                    if exitstatus is None or exitstatus == 0:
                        status = 'OK'
                    else:
                        status = exitstatus

                    # get uninstallkey from setup module (string or array of strings)
                    if hasattr(setup,'uninstallkey'):
                        new_uninstall_key = ensure_list(setup.uninstallkey)[:]
                        # check that uninstallkey(s) are in registry
                        if not self.dry_run:
                            key_errors = []
                            for key in new_uninstall_key:
                                if not setuphelpers.uninstall_key_exists(uninstallkey=key):
                                    key_errors.append(key)
                            if key_errors:
                                if len(key_errors)>1:
                                    raise EWaptException(u'The uninstall keys: \n%s\n have not been found in system registry after softwares installation.' % ('\n'.join(key_errors),))
                                else:
                                    raise EWaptException(u'The uninstall key: %s has not been found in system registry after software installation.' % (' '.join(key_errors),))

                    else:
                        new_uninstall = self.registry_uninstall_snapshot()
                        new_uninstall_key = [ k for k in new_uninstall if not k in previous_uninstall]

                    # get uninstallstring from setup module (string or array of strings)
                    if hasattr(setup,'uninstallstring'):
                        uninstallstring = setup.uninstallstring[:]
                    else:
                        uninstallstring = None
                    logger.info(u'  uninstall keys : %s' % (new_uninstall_key,))
                    logger.info(u'  uninstall strings : %s' % (uninstallstring,))

                    logger.info(u"Install script finished with status %s" % status)
                else:
                    logger.info(u'No setup.py')
                    status = 'OK'

            finally:
                if istemporary:
                    os.chdir(previous_cwd)
                    logger.debug(u"Cleaning package tmp dir")
                    # trying 3 times to remove
                    cnt = 3
                    while cnt>0:
                        try:
                            shutil.rmtree(packagetempdir)
                            break
                        except:
                            cnt -= 1
                            time.sleep(2)
                    else:
                        logger.warning(u"Unable to clean tmp dir")

            # rowid,install_status,install_output,uninstall_key=None,uninstall_string=None
            self.waptdb.update_install_status(install_id,
                install_status=status,
                install_output='',
                uninstall_key=str(new_uninstall_key) if new_uninstall_key else '',
                uninstall_string=str(uninstallstring) if uninstallstring else '')
            return self.waptdb.install_status(install_id)

        except Exception as e:
            if install_id:
                try:
                    self.waptdb.update_install_status(install_id,'ERROR',ensure_unicode(e))
                except Exception as e2:
                    logger.critical(ensure_unicode(e2))
            else:
                logger.critical(ensure_unicode(e))
            raise e
        finally:
            gc.collect()
            if 'setup' in dir() and setup is not None:
                setup_name = setup.__name__[:]
                logger.debug('Removing module: %s, refcnt: %s'%(setup_name,sys.getrefcount(setup)))
                del setup
                if setup_name in sys.modules:
                    del sys.modules[setup_name]

            sys.stdout = old_stdout
            sys.stderr = old_stderr
            sys.path = oldpath

            self.store_upgrade_status()
            self.runstatus=''


    def call_setup_hook(self,wapt_package_dir,hook_name='update_package',*args,**kwargs):
        """Install a single wapt package given its WAPT filename.
        return install status"""
        install_id = None
        old_hdlr = None
        old_stdout = None
        old_stderr = None

        if not self.is_wapt_package_development_dir(wapt_package_dir):
            raise EWaptNotAPackage(u'%s is not a package development directory, aborting.' % ensure_unicode(wapt_package_dir))

        entry = PackageEntry(waptfile=wapt_package_dir)

        # we  record old sys.path as we will include current setup.py
        oldpath = sys.path

        try:
            previous_cwd = os.getcwdu()
            setup_filename = os.path.join( wapt_package_dir,'setup.py')

            # take in account the case we have no setup.py
            if os.path.isfile(setup_filename):
                os.chdir(os.path.dirname(setup_filename))
                if not os.getcwdu() in sys.path:
                    sys.path.append(os.getcwdu())

                # import the setup module from package file
                logger.info(u"  sourcing install file %s " % ensure_unicode(setup_filename) )
                setup = import_setup(setup_filename)
                hook_func = getattr(setup,hook_name,None)
                if hook_func is None:
                    raise Exception('Function %s can not be found in setup module' % hook_name)

                # be sure some minimal functions are available in setup module at install step
                setattr(setup,'basedir',os.path.dirname(setup_filename))
                # redefine run to add reference to wapt.pidlist
                setattr(setup,'run',self.run)
                setattr(setup,'run_notfatal',self.run_notfatal)
                setattr(setup,'WAPT',self)
                setattr(setup,'control',entry)
                setattr(setup,'language',self.language)
                setattr(setup,'user',self.user)
                setattr(setup,'usergroups',self.usergroups)

                try:
                    logger.info(u"  executing setup.%s(%s,%s) " % (hook_name,repr(args),repr(kwargs)))
                    exitstatus = hook_func(*args,**kwargs)
                except Exception as e:
                    logger.critical(u'Fatal error in %s function: %s:\n%s' % (hook_name,ensure_unicode(e),ensure_unicode(traceback.format_exc())))
                    raise
                return exitstatus
        finally:
            os.chdir(previous_cwd)
            gc.collect()
            if 'setup' in dir() and setup is not None:
                setup_name = setup.__name__[:]
                logger.debug('Removing module: %s, refcnt: %s'%(setup_name,sys.getrefcount(setup)))
                del setup
                if setup_name in sys.modules:
                    del sys.modules[setup_name]

            sys.path = oldpath

    def running_tasks(self):
        """return current install tasks"""
        running = self.waptdb.query_package_entry("""\
           select * from wapt_localstatus
              where install_status in ('INIT','DOWNLOAD','RUNNING')
           """ )
        return running

    def error_packages(self):
        """return install tasks with error status"""
        q = self.waptdb.query_package_entry("""\
           select * from wapt_localstatus
              where install_status in ('ERROR')
           """ )
        return q

    def store_upgrade_status(self,upgrades=None):
        """Stores in DB the current pending upgrades and running installs for
          query by waptservice"""
        try:
            status={
                "running_tasks": [ "%s : %s" % (p.asrequirement(),p.install_status) for p in self.running_tasks()],
                "errors": [ "%s : %s" % (p.asrequirement(),p.install_status) for p in self.error_packages()],
                "date":datetime2isodate(),
                }
            if upgrades is None:
                upgrades = self.list_upgrade()

            status["upgrades"] = upgrades['upgrade']+upgrades['install']+upgrades['additional']
            status["pending"] = upgrades
            logger.debug(u"store status in DB")
            self.write_param('last_update_status',jsondump(status))
            return status
        except Exception as e:
            logger.critical(u'Unable to store status of update in DB : %s'% ensure_unicode(e))
            if logger.level == logging.DEBUG:
                raise

    def get_sources(self,package):
        """Download sources of package (if referenced in package as a https svn)
           in the current directory"""
        sources_url = None
        entry = None
        entries = self.waptdb.packages_matching(package)
        if entries:
            entry = entries[-1]
            if entry.sources:
                sources_url = entry.sources
        if not sources_url:
            if self.config.has_option('global','default_sources_url'):
                sources_url = self.config.get('global','default_sources_url') % {'packagename':package}

        if not sources_url:
            raise Exception('No sources defined in package control file and no default_sources_url in config file')
        if "PROGRAMW6432" in os.environ:
            svncmd = os.path.join(os.environ['PROGRAMW6432'],'TortoiseSVN','bin','svn.exe')
        else:
            svncmd = os.path.join(os.environ['PROGRAMFILES'],'TortoiseSVN','bin','svn.exe')
        logger.debug(u'svn command : %s'% svncmd)
        if not os.path.isfile(svncmd):
            raise Exception(u'svn.exe command not available, please install TortoiseSVN with commandline tools')

        # checkout directory
        if entry:
            co_dir = self.get_default_development_dir(entry.package, section = entry.section)
        else:
            co_dir = self.get_default_development_dir(package)

        logger.info(u'sources : %s'% sources_url)
        logger.info(u'checkout dir : %s'% co_dir)
        # if already checked out...
        if os.path.isdir(os.path.join(co_dir,'.svn')):
            print(self.run(u'"%s" up "%s"' % (svncmd,co_dir)))
        else:
            print(self.run(u'"%s" co "%s" "%s"' % (svncmd,sources_url,co_dir)))
        return co_dir

    def last_install_log(self,packagename):
        r"""Get the printed output of the last install of package named packagename

        Args:
            packagename (str): name of package to query
        Returns:
            dict: {status,log} of the last install of a package

        >>> w = Wapt()
        >>> w.last_install_log('tis-7zip')
        ???
        {'status': u'OK', 'log': u'Installing 7-Zip 9.38.0-1\n7-Zip already installed, skipping msi install\n'}

        """
        q = self.waptdb.query("""\
           select install_status,install_output from wapt_localstatus
            where package=? order by install_date desc limit 1
           """ , (packagename,) )
        if not q:
            raise Exception("Package %s not found in local DB status" % packagename)
        return {"status" : q[0]['install_status'], "log":q[0]['install_output']}

    def cleanup(self,obsolete_only=False):
        """Remove cached WAPT files from local disk

        Args:
           obsolete_only (boolean):  If True, remove packages which are either no more available,
                                     or installed at a equal or newer version

        Returns:
            list: list of filenames of removed packages

        >>> wapt = Wapt(config_filename='c:/wapt/wapt-get.ini')
        >>> l = wapt.download_packages(wapt.check_downloads())
        >>> res = wapt.cleanup(True)
        """
        result = []
        logger.info(u'Cleaning up WAPT cache directory')
        cachepath = self.package_cache_dir

        upgrade_actions = self.list_upgrade()
        futures =   upgrade_actions['install']+\
                    upgrade_actions['upgrade']+\
                    upgrade_actions['additional']
        def in_futures(pe):
            for p in futures:
                if pe.match(p):
                    return True
            return False

        for f in glob.glob(os.path.join(cachepath,'*.wapt')):
            if os.path.isfile(f):
                can_remove = True
                if obsolete_only:
                    try:
                        # check if cached package could be installed at next ugrade
                        pe = PackageEntry().load_control_from_wapt(f)
                        pe_installed = self.is_installed(pe.package)
                        can_remove = not in_futures(pe) and ((pe_installed and pe <= pe_installed) or not self.is_available(pe.asrequirement()))
                    except:
                        # if error... control file in wapt file is corrupted.
                        continue
                if can_remove:
                    logger.debug(u'Removing %s' % f)
                    try:
                        os.remove(f)
                        result.append(f)
                    except Exception as e:
                        logger.warning(u'Unable to remove %s : %s' % (f,ensure_unicode(e)))
        return result

    def _update_db(self,repo,force=False,filter_on_host_cap=True):
        """Get Packages from http repo and update local package database
        return last-update header

        The local status DB is updated. Date of index is stored in params table
        for further checks.

        Args:
            force (bool): get index from remote repo even if creation date is not newer
                          than the datetime stored in local status database
            waptdb (WaptDB): instance of Wapt status database.

        Returns:
            isodatetime: date of Packages index

        >>> import common
        >>> repo = common.WaptRepo('wapt','http://wapt/wapt')
        >>> localdb = common.WaptDB('c:/wapt/db/waptdb.sqlite')
        >>> last_update = repo.is_available()
        >>> repo.update_db(waptdb=localdb) == last_update
        True
        """

        result = None
        last_modified = self.waptdb.get_param('last-%s'%(repo.repo_url[:59]))
        last_url = self.waptdb.get_param('last-url-%s' % repo.name)

        # Check if updated
        if force or repo.repo_url != last_url or repo.need_update(last_modified):
            os_version = setuphelpers.windows_version()
            old_status = repo.invalidate_packages_cache()

            with self.waptdb:
                try:
                    logger.debug(u'Read remote Packages index file %s' % repo.packages_url)
                    last_modified = repo.packages_date

                    self.waptdb.purge_repo(repo.name)
                    for package in repo.packages:
                        if filter_on_host_cap:
                            if not self.is_locally_allowed_package(package):
                                logger.info('Discarding %s on repo "%s" because of local whitelist of blacklist rules' % (package.asrequirement(),repo.name))
                                continue
                            if package.min_wapt_version and Version(package.min_wapt_version)>Version(setuphelpers.__version__):
                                logger.debug('Skipping package %s on repo %s, requires a newer Wapt agent. Minimum version: %s' % (package.asrequirement(),repo.name,package.min_wapt_version))
                                continue
                            if (package.locale and package.locale != 'all') and self.locales and not list_intersection(ensure_list(package.locale),self.locales):
                                logger.debug('Skipping package %s on repo %s, designed for locale %s' %(package.asrequirement(),repo.name,package.locale))
                                continue
                            if package.maturity and self.maturities and not package.maturity in self.maturities:
                                logger.debug('Skipping package %s on repo %s, maturity  %s not enabled on this host' %(package.asrequirement(),repo.name,package.maturity))
                                continue
                            if package.target_os and package.target_os != 'windows':
                                logger.debug('Skipping package %s on repo %s, designed for OS %s' %(package.asrequirement(),repo.name,package.target_os))
                                continue
                            if package.min_os_version and os_version < Version(package.min_os_version):
                                logger.debug('Discarding package %s on repo %s, requires OS version > %s' % (package.asrequirement(),repo.name,package.min_os_version))
                                continue
                            if package.max_os_version and os_version > Version(package.max_os_version):
                                logger.debug('Discarding package %s on repo %s, requires OS version < %s' % (package.asrequirement(),repo.name,package.max_os_version))
                                continue
                            if package.architecture == 'x64' and not setuphelpers.iswin64():
                                logger.debug('Discarding package %s on repo %s, requires OS with x64 architecture' % (package.asrequirement(),repo.name,))
                                continue
                            if package.architecture == 'x86' and setuphelpers.iswin64():
                                logger.debug('Discarding package %s on repo %s, target OS with x86-32 architecture' % (package.asrequirement(),repo.name,))
                                continue

                        try:
                            self.waptdb.add_package_entry(package,self.language)
                        except Exception as e:
                            logger.critical('Invalid signature for package control entry %s on repo %s : discarding : %s' % (package.asrequirement(),repo.name,e) )

                    logger.debug(u'Storing last-modified header for repo_url %s : %s' % (repo.repo_url,repo.packages_date))
                    self.waptdb.set_param('last-%s' % repo.repo_url[:59],repo.packages_date)
                    self.waptdb.set_param('last-url-%s' % repo.name, repo.repo_url)
                    return last_modified
                except Exception as e:
                    logger.info(u'Unable to update repository status of %s, error %s'%(repo._repo_url,e))
                    # put back cached status data
                    for (k,v) in old_status.iteritems():
                        setattr(repo,k,v)
                    raise
        else:
            return self.waptdb.get_param('last-%s' % repo.repo_url[:59])

    def get_host_architecture(self):
        if setuphelpers.iswin64():
            return 'x64'
        else:
            return 'x86'

    def get_host_locales(self):
        return ensure_list(self.locales)

    def get_host_site(self):
        return setuphelpers.registry_readstring(HKEY_LOCAL_MACHINE,r'SOFTWARE\Microsoft\Windows\CurrentVersion\Group Policy\State\Machine','Site-Name')

    def host_capabilities_fingerprint(self):
        """Return a fingerprint representing the current capabilities of host
        This includes host certificate,architecture,locale,authorized certificates

        Returns:
            str

        """
        host_capa = dict(
            host_cert=self.get_host_certificate().fingerprint,
            host_arch=self.get_host_architecture(),
            authorized_certs=[c.fingerprint for c in self.authorized_certificates()],
            #authorized_maturities=self.get_host_maturities(),
            wapt_version=setuphelpers.__version__,
            host_dn=self.host_dn,
            host_site=self.get_host_site(),
            packages_blacklist=self.packages_blacklist,
            packages_whitelist=self.packages_whitelist,
            host_locales=self.locales,
            host_language=self.language,
            host_maturities=self.maturities,
        )
        return hashlib.sha256(jsondump(host_capa)).hexdigest()

    def is_locally_allowed_package(self,package):
        """Return True if package is not in blacklist and is in whitelist if whitelist is not None
        packages_whitelist and packages_blacklist are list of package name wildcards (file style wildcards)
        blacklist is taken in account first if defined.
        whitelist is taken in acoount if not None, else all not blacklisted package names are allowed.
        """
        if self.packages_blacklist is not None:
            for bl in self.packages_blacklist:
                if glob.fnmatch.fnmatch(package.package,bl):
                    return False
        if self.packages_whitelist is None:
            return True
        else:
            for wl in self.packages_whitelist:
                if glob.fnmatch.fnmatch(package.package,wl):
                    return True
        return False

    def _update_repos_list(self,force=False,filter_on_host_cap=True):
        """update the packages database with Packages files from the Wapt repos list
        removes obsolete records for repositories which are no more referenced

        Args:
            force : update repository even if date of packages index is same as
                    last retrieved date

        Returns:
            dict:   update_db results for each repository name
                    which has been accessed.

        >>> wapt = Wapt(config_filename = 'c:/tranquilit/wapt/tests/wapt-get.ini' )
        >>> res = wapt._update_repos_list()
        {'wapt': '2018-02-13T11:22:00', 'wapt-host': u'2018-02-09T10:55:04'}
        """
        with self.waptdb:
            result = {}
            # force update if host capabilities have changed and requires a new filering of packages
            new_capa = self.host_capabilities_fingerprint()
            old_capa = self.read_param('host_capabilities_fingerprint')
            if not force and old_capa != new_capa:
                logger.info('Host capabilities have changed since last update, forcing update')
                force = True
            logger.debug(u'Remove unknown repositories from packages table and params (%s)' %(','.join('"%s"'% r.name for r in self.repositories),)  )
            self.waptdb.db.execute('delete from wapt_package where repo not in (%s)' % (','.join('"%s"'% r.name for r in self.repositories)))
            self.waptdb.db.execute('delete from wapt_params where name like "last-http%%" and name not in (%s)' % (','.join('"last-%s"'% r.repo_url for r in self.repositories)))
            self.waptdb.db.execute('delete from wapt_params where name like "last-url-%%" and name not in (%s)' % (','.join('"last-url-%s"'% r.name for r in self.repositories)))
            for repo in self.repositories:
                # if auto discover, repo_url can be None if no network.
                if repo.repo_url:
                    try:
                        logger.info(u'Getting packages from %s' % repo.repo_url)
                        result[repo.name] = self._update_db(repo,force=force,filter_on_host_cap=filter_on_host_cap)
                    except Exception as e:
                        logger.critical(u'Error getting Packages index from %s : %s' % (repo.repo_url,ensure_unicode(e)))
                else:
                    logger.info('No location found for repository %s, skipping' % (repo.name))
            self.write_param('host_capabilities_fingerprint',new_capa)
        return result


    def update(self,force=False,register=True,filter_on_host_cap=True):
        """Update local database with packages definition from repositories

        Args:
            force (boolean):    update even if Packages index on repository has not been
                                updated since last update (based on http headers)
            register (boolean): Send informations about status of local packages to waptserver
        .. versionadded 1.3.10::
            filter_on_host_cap (boolean) : restrict list of retrieved packages to those matching current os / architecture

        Returns;
            list of (host package entry,entry date on server)

        Returns:
            dict: {"added","removed","count","repos","upgrades","date"}

        >>> wapt = Wapt(config_filename='c:/wapt/wapt-get.ini')
        >>> updates = wapt.update()
        >>> 'count' in updates and 'added' in updates and 'upgrades' in updates and 'date' in updates and 'removed' in updates
        True

        """
        previous = self.waptdb.known_packages()
        # (main repo is at the end so that it will used in priority)
        self._update_repos_list(force=force,filter_on_host_cap=filter_on_host_cap)

        current = self.waptdb.known_packages()
        result = {
            "added":   [ p for p in current if not p in previous ],
            "removed": [ p for p in previous if not p in current],
            "count" : len(current),
            "repos" : [r.repo_url for r in self.repositories],
            "upgrades": self.list_upgrade(),
            "date":datetime2isodate(),
            }

        self.store_upgrade_status(result['upgrades'])

        if self.waptserver and not self.disable_update_server_status and register:
            try:
                self.update_server_status()
            except Exception as e:
                logger.info(u'Unable to contact server to register current packages status')
                logger.debug(u'Unable to update server with current status : %s' % ensure_unicode(e))
                if logger.level == logging.DEBUG:
                    raise
        return result

    def update_crls(self,force=False):
        # retrieve CRL
        # TODO : to be moved to an abstracted wapt https client
        crl_dir = setuphelpers.makepath(self.wapt_base_dir,'ssl','crl')
        result = []
        for cert in self.cabundle.certificates():
            crl_urls = cert.crl_urls()
            for url in crl_urls:
                crl_filename = setuphelpers.makepath(crl_dir,sha256_for_data(str(url))+'.crl')
                if os.path.isfile(crl_filename):
                    ssl_crl = SSLCRL(crl_filename)
                else:
                    ssl_crl = None

                if force or not ssl_crl or ssl_crl.next_update > datetime.datetime.utcnow():
                    try:
                        # need update
                        if not os.path.isdir(crl_dir):
                            os.makedirs(crl_dir)
                        logger.debug('Download CRL %s' % (url,))
                        wget(url,target=crl_filename)
                        ssl_crl = SSLCRL(crl_filename)
                        result.append(ssl_crl)
                    except Exception as e:
                        logger.warning('Unable to download CRL from %s: %s' % (url,repr(e)))
                        if ssl_crl:
                            result.append(ssl_crl)
                        pass
                elif ssl_crl:
                    # not changed
                    result.append(ssl_crl)
        return result


    def check_depends(self,apackages,forceupgrade=False,force=False,assume_removed=[]):
        """Given a list of packagename or requirement "name (=version)",
        return a dictionnary of {'additional' 'upgrade' 'install' 'skipped' 'unavailable','remove'} of
        [packagerequest,matching PackageEntry]

        Args:
            apackages (str or list): list of packages for which to check missing dependencies.
            forceupgrade (boolean): if True, check if the current installed packages is the latest available
            force (boolean): if True, install the latest version even if the package is already there and match the requirement
            assume_removed (list): list of packagename which are assumed to be absent even if they are actually installed to check the
                                    consequences of removal of packages, implies force=True
        Returns:
            dict : {'additional' 'upgrade' 'install' 'skipped' 'unavailable', 'remove'} with list of [packagerequest,matching PackageEntry]

        """
        if apackages is None:
            apackages = []
        # for csv string list of dependencies
        apackages = ensure_list(apackages)

        # check if all members are strings packages requirements "package_name(=version)"
        apackages = [isinstance(p,PackageEntry) and p.asrequirement() or p for p in apackages]

        if not isinstance(assume_removed,list):
            assume_removed = [assume_removed]
        if assume_removed:
            force=True
        # packages to install after skipping already installed ones
        skipped = []
        unavailable = []
        additional_install = []
        to_upgrade = []
        to_remove = []
        packages = []

        # search for most recent matching package to install
        for request in apackages:
            # get the current installed package matching the request
            old_matches = self.waptdb.installed_matching(request)

            # removes "assumed removed" packages
            if old_matches:
                for packagename in assume_removed:
                    if old_matches.match(packagename):
                        old_matches = None
                        break

            # current installed matches
            if not force and old_matches and not forceupgrade:
                skipped.append((request,old_matches))
            else:
                new_availables = self.waptdb.packages_matching(request)
                if new_availables:
                    if force or not old_matches or (forceupgrade and old_matches < new_availables[-1]):
                        if not (request,new_availables[-1]) in packages:
                            packages.append((request,new_availables[-1]))
                    else:
                        skipped.append((request,old_matches))
                else:
                    if (request,None) not in unavailable:
                        unavailable.append((request,None))

        # get dependencies of not installed top packages
        if forceupgrade:
            (depends,missing) = self.waptdb.build_depends(apackages)
        else:
            (depends,missing) = self.waptdb.build_depends([p[0] for p in packages])

        for p in missing:
            if (p,None) not in unavailable:
                unavailable.append((p,None))

        # search for most recent matching package to install
        for request in depends:
            # get the current installed package matching the request
            old_matches = self.waptdb.installed_matching(request)

            # removes "assumed removed" packages
            if old_matches:
                for packagename in assume_removed:
                    if old_matches.match(packagename):
                        old_matches = None
                        break

            # current installed matches
            if not force and old_matches:
                skipped.append((request,old_matches))
            else:
                # check if installable or upgradable ?
                new_availables = self.waptdb.packages_matching(request)
                if new_availables:
                    if not old_matches or (forceupgrade and old_matches < new_availables[-1]):
                        additional_install.append((request,new_availables[-1]))
                    else:
                        skipped.append((request,old_matches))
                else:
                    unavailable.append((request,None))

        # check new conflicts which should force removal
        all_new = additional_install+to_upgrade+packages

        def remove_matching(package,req_pe_list):
            todel = []
            for req,pe in req_pe_list:
                if pe.match(package):
                    todel.append((req,pe))
            for e in todel:
                req_pe_list.remove(e)

        for (request,pe) in all_new:
            conflicts = ensure_list(pe.conflicts)
            for conflict in conflicts:
                installed_conflict = self.waptdb.installed_matching(conflict)
                if installed_conflict and not ((conflict,installed_conflict)) in to_remove:
                    to_remove.append((conflict,installed_conflict))
                remove_matching(conflict,to_upgrade)
                remove_matching(conflict,additional_install)
                remove_matching(conflict,skipped)


        result =  {'additional':additional_install,'upgrade':to_upgrade,'install':packages,'skipped':skipped,'unavailable':unavailable,'remove':to_remove}
        return result

    def check_remove(self,apackages):
        """Return a list of additional package to remove if apackages are removed

        Args:
            apackages (str or list): list of packages fr which parent dependencies will be checked.

        Returns:
            list: list of package requirements with broken dependencies

        """
        if not isinstance(apackages,list):
            apackages = [apackages]
        result = []
        installed = [ p.asrequirement() for p in self.installed().values() if p.asrequirement() not in apackages ]
        for packagename in installed:
            # test for each installed package if the removal would imply a reinstall
            test = self.check_depends(packagename,assume_removed=apackages)
            # get package names only
            reinstall = [ p[0] for p in (test['upgrade'] + test['additional'])]
            for p in reinstall:
                if p in apackages and not packagename in result:
                    result.append(packagename)
        return result

    def check_install(self,apackages=None,force=True,forceupgrade=True):
        """Return a list of actions required for install of apackages list of packages
        if apackages is None, check for all pending updates.

        Args:
            apackages (str or list): list of packages or None to check pending install/upgrades
            force (boolean): if True, already installed package listed in apackages
                                will be considered to be reinstalled
            forceupgrade: if True, all dependencies are upgraded to latest version,
                          even if current version comply with depends requirements
        Returns:
            dict: with keys ['skipped', 'additional', 'remove', 'upgrade', 'install', 'unavailable'] and list of
                        (package requirements, PackageEntry)

        """
        result = []
        if apackages is None:
            actions = self.list_upgrade()
            apackages = actions['install']+actions['additional']+actions['upgrade']
        elif isinstance(apackages,(str,unicode)):
            apackages = ensure_list(apackages)
        elif isinstance(apackages,list):
            # ensure that apackages is a list of package requirements (strings)
            new_apackages = []
            for p in apackages:
                if isinstance(p,PackageEntry):
                    new_apackages.append(p.asrequirement())
                else:
                    new_apackages.append(p)
            apackages = new_apackages
        actions = self.check_depends(apackages,force=force,forceupgrade=forceupgrade)
        return  actions

    def install(self,apackages,
            force=False,
            params_dict = {},
            download_only=False,
            usecache=True,
            printhook=None,
            installed_by=None):
        """Install a list of packages and its dependencies
        removes first packages which are in conflicts package attribute

        Returns a dictionary of (package requirement,package) with 'install','skipped','additional'

        Args:
            apackages (list or str): list of packages requirements "packagename(=version)" or list of PackageEntry.
            force (bool) : reinstalls the packages even if it is already installed
            params_dict (dict) : parameters passed to the install() procedure in the packages setup.py of all packages
                          as params variables and as "setup module" attributes
            download_only (bool) : don't install package, but only download them
            usecache (bool) : use the already downloaded packages if available in cache directory
            printhook (func) : hook for progress print

        Returns:
            dict: with keys ['skipped', 'additional', 'remove', 'upgrade', 'install', 'unavailable'] and list of
                        (package requirements, PackageEntry)

        >>> wapt = Wapt(config_filename='c:/tranquilit/wapt/tests/wapt-get.ini')
        >>> def nullhook(*args):
        ...     pass
        >>> res = wapt.install(['tis-wapttest'],usecache=False,printhook=nullhook,params_dict=dict(company='toto'))
        >>> isinstance(res['upgrade'],list) and isinstance(res['errors'],list) and isinstance(res['additional'],list) and isinstance(res['install'],list) and isinstance(res['unavailable'],list)
        True
        >>> res = wapt.remove('tis-wapttest')
        >>> res == {'removed': ['tis-wapttest'], 'errors': []}
        True
        """
        if not isinstance(apackages,list):
            apackages = [apackages]

        # ensure that apackages is a list of package requirements (strings)
        new_apackages = []
        for p in apackages:
            if isinstance(p,PackageEntry):
                new_apackages.append(p.asrequirement())
            else:
                new_apackages.append(p)
        apackages = new_apackages

        actions = self.check_depends(apackages,force=force or download_only,forceupgrade=True)
        actions['errors']=[]

        skipped = actions['skipped']
        additional_install = actions['additional']
        to_upgrade = actions['upgrade']
        packages = actions['install']
        missing = actions['unavailable']

        # removal from conflicts
        to_remove = actions['remove']
        for (request,pe) in to_remove:
            logger.info('Removing conflicting package %s'%request)
            try:
                res = self.remove(request,force=True)
                actions['errors'].extend(res['errors'])
                if res['errors']:
                    logger.warning(u'Error removing %s:%s'%(request,ensure_unicode(res['errors'])))
            except Exception as e:
                logger.critical(u'Error removing %s:%s'%(request,ensure_unicode(e)))

        to_install = []
        to_install.extend(additional_install)
        to_install.extend(to_upgrade)
        to_install.extend(packages)

        # get package entries to install to_install is a list of (request,package)
        packages = [ p[1] for p in to_install ]

        downloaded = self.download_packages(packages,usecache=usecache,printhook=printhook)
        if downloaded.get('errors',[]):
            logger.critical(u'Error downloading some files : %s'%(downloaded['errors'],))
            for request in downloaded.get('errors',[]):
                actions['errors'].append([request,None])

        # check downloaded packages signatures and merge control data in local database
        for fname in downloaded['downloaded'] + downloaded['skipped']:
            pe = PackageEntry(waptfile = fname)
            pe.check_control_signature(self.cabundle)

        actions['downloads'] = downloaded
        logger.debug(u'Downloaded : %s' % (downloaded,))

        def full_fname(packagefilename):
            return os.path.join(self.package_cache_dir,packagefilename)

        if not download_only:
            # switch to manual mode
            for (request,p) in skipped:
                if request in apackages and not p.explicit_by:
                    logger.info(u'switch to manual mode for %s' % (request,))
                    self.waptdb.switch_to_explicit_mode(p.package,installed_by or self.user)

            for (request,p) in to_install:
                try:
                    if not os.path.isfile(full_fname(p.filename)):
                        raise EWaptDownloadError('Package file %s not downloaded properly.' % p.filename)
                    print(u"Installing %s" % (p.package,))
                    result = self.install_wapt(full_fname(p.filename),
                        params_dict = params_dict,
                        explicit_by=(installed_by or self.user) if request in apackages else None
                        )
                    if result:
                        for k in result.as_dict():
                            p[k] = result[k]

                    if not result or result['install_status'] != 'OK':
                        actions['errors'].append([request,p])
                        logger.critical(u'Package %s not installed due to errors' %(request,))
                except Exception as e:
                    actions['errors'].append([request,p])
                    logger.critical(u'Package %s not installed due to errors : %s' %(request,ensure_unicode(e)))
                    if logger.level == logging.DEBUG:
                        raise
            return actions
        else:
            logger.info(u'Download only, no install performed')
            return actions

    def download_packages(self,package_requests,usecache=True,printhook=None):
        r"""Download a list of packages (requests are of the form packagename (>version) )
        returns a dict of {"downloaded,"skipped","errors"}

        Args:
            package_requests (str or list): list of packages to prefetch
            usecache (boolean) : if True, don't download package if already in cache
            printhook (func) : callback with signature report(received,total,speed,url) to display progress

        Returns:
            dict: with keys {"downloaded,"skipped","errors","packages"} and list of PackageEntry.

        >>> wapt = Wapt(config_filename='c:/wapt/wapt-get.ini')
        >>> def nullhook(*args):
        ...     pass
        >>> wapt.download_packages(['tis-firefox','tis-waptdev'],usecache=False,printhook=nullhook)
        {'downloaded': [u'c:/wapt\\cache\\tis-firefox_37.0.2-9_all.wapt', u'c:/wapt\\cache\\tis-waptdev.wapt'], 'skipped': [], 'errors': []}
        """
        if not isinstance(package_requests,(list,tuple)):
            package_requests = [ package_requests ]
        downloaded = []
        skipped = []
        errors = []
        packages = []

        for p in package_requests:
            if isinstance(p,str) or isinstance(p,unicode):
                mp = self.waptdb.packages_matching(p)
                if mp:
                    packages.append(mp[-1])
                else:
                    errors.append((p,u'Unavailable package %s' % (p,)))
                    logger.critical(u'Unavailable package %s' % (p,))
            elif isinstance(p,PackageEntry):
                packages.append(p)
            elif isinstance(p,list) or isinstance(p,tuple):
                packages.append(self.waptdb.package_entry_from_db(p[0],version_min=p[1],version_max=p[1]))
            else:
                raise Exception('Invalid package request %s' % p)

        for entry in packages:
            self.check_cancelled()


            def report(received,total,speed,url):
                self.check_cancelled()
                try:
                    if total>1:
                        stat = u'%s : %i / %i (%.0f%%) (%.0f KB/s)\r' % (url,received,total,100.0*received/total, speed)
                        print(stat)
                    else:
                        stat = ''
                    self.runstatus='Downloading %s : %s' % (entry.package,stat)
                except:
                    self.runstatus='Downloading %s' % (entry.package,)
            """
            if not printhook:
                printhook = report
            """
            res = self.get_repo(entry.repo).download_packages(entry,
                target_dir=self.package_cache_dir,
                usecache=usecache,
                printhook=printhook)

            downloaded.extend(res['downloaded'])
            skipped.extend(res['skipped'])
            errors.extend(res['errors'])

        return {"downloaded":downloaded,"skipped":skipped,"errors":errors,"packages":packages}

    def get_repo(self,repo_name):
        for r in self.repositories:
            if r.name == repo_name:
                return r
        return None

    def remove(self,packages_list,force=False):
        """Removes a package giving its package name, unregister from local status DB

        Args:
            packages_list (str or list or path): packages to remove (package name,
                            list of package requirement, package entry or development directory)
            force : if True, unregister package from local status database, even if uninstall has failed

        Returns:
            dict: {'errors': [], 'removed': []}

        """
        result = {'removed':[],'errors':[]}
        packages_list = ensure_list(packages_list)
        for package in packages_list:
            try:
                self.check_cancelled()
                # development mode, remove a package by its directory
                if isinstance(package,(str,unicode)) and os.path.isfile(os.path.join(package,'WAPT','control')):
                    package = PackageEntry().load_control_from_wapt(package).package
                elif isinstance(package,PackageEntry):
                    package = package.package
                else:
                    pe = self.is_installed(package)
                    if pe:
                        package = pe.package

                q = self.waptdb.query(u"""\
                   select * from wapt_localstatus
                    where package=?
                   """ , (package,))
                if not q:
                    logger.debug(u"Package %s not installed, removal aborted" % package)
                    return result

                # several versions installed of the same package... ?
                for mydict in q:
                    self.runstatus="Removing package %s version %s from computer..." % (mydict['package'],mydict['version'])

                    # removes recursively meta packages which are not satisfied anymore
                    additional_removes = self.check_remove(package)

                    if mydict.get('impacted_process',None):
                        setuphelpers.killalltasks(ensure_list(mydict['impacted_process']))

                    if mydict['uninstall_string']:
                        if mydict['uninstall_string'][0] not in ['[','"',"'"]:
                            guids = mydict['uninstall_string']
                        else:
                            try:
                                guids = eval(mydict['uninstall_string'])
                            except:
                                guids = mydict['uninstall_string']
                        if isinstance(guids,(unicode,str)):
                            guids = [guids]
                        for guid in guids:
                            if guid:
                                try:
                                    logger.info(u'Running %s' % guid)
                                    logger.info(self.run(guid))
                                except Exception as e:
                                    logger.warning(u"Warning : %s" % ensure_unicode(e))

                    elif mydict['uninstall_key']:
                        if mydict['uninstall_key'][0] not in ['[','"',"'"]:
                            guids = mydict['uninstall_key']
                        else:
                            try:
                                guids = eval(mydict['uninstall_key'])
                            except:
                                guids = mydict['uninstall_key']

                        if isinstance(guids,(unicode,str)):
                            guids = [guids]

                        for guid in guids:
                            if guid:
                                try:
                                    uninstall_cmd =''
                                    uninstall_cmd = self.uninstall_cmd(guid)
                                    if uninstall_cmd:
                                        logger.info(u'Launch uninstall cmd %s' % (uninstall_cmd,))
                                        # if running porcesses, kill them before launching uninstaller
                                        print(self.run(uninstall_cmd))
                                except Exception as e:
                                    logger.critical(u"Critical error during uninstall cmd %s: %s" % (uninstall_cmd,ensure_unicode(e)))
                                    result['errors'].append(package)
                                    if not force:
                                        raise

                    else:
                        logger.debug(u'uninstall key not registered in local DB status.')

                    if mydict['install_status'] != 'ERROR':
                        try:
                            self.uninstall(package)
                        except Exception as e:
                            logger.critical(u'Error running uninstall script: %s'%e)
                            result['errors'].append(package)

                    logger.info(u'Remove status record from local DB for %s' % package)
                    self.waptdb.remove_install_status(package)
                    result['removed'].append(package)

                    if reversed(additional_removes):
                        logger.info(u'Additional packages to remove : %s' % additional_removes)
                        for apackage in additional_removes:
                            res = self.remove(apackage,force=True)
                            result['removed'].extend(res['removed'])
                            result['errors'].extend(res['errors'])

                return result
            finally:
                self.store_upgrade_status()
                self.runstatus=''

    def host_packagename(self):
        """Return package name for current computer"""
        #return "%s" % (setuphelpers.get_hostname().lower())
        return "%s" % (self.host_uuid,)

    def get_host_packages_names(self):
        """Return list of implicit host package names based on computer UUID and AD Org Units

        Returns:
            list: list of str package names.
        """
        """Return list of implicit available host packages based on computer UUID and AD Org Units

        Returns:
            list: list of PackageEntry.
        """
        result = []
        host_package = self.host_packagename()
        result.append(host_package)
        previous_dn_part_type = ''
        host_dn = self.host_dn
        if host_dn:
            dn_parts = host_dn.split(',')
            for i in range(1,len(dn_parts)):
                dn_part = dn_parts[i]
                dn_part_type,value = dn_part.split('=',1)
                if dn_part_type.lower() == 'dc' and  dn_part_type == previous_dn_part_type:
                    break
                level_dn = ','.join(dn_parts[i:])
                result.append(level_dn)
                previous_dn_part_type = dn_part_type
        return result

    def get_host_packages(self):
        """Return list of implicit available host packages based on computer UUID and AD Org Units

        Returns:
            list: list of PackageEntry.
        """
        result = []
        package_names = self.get_host_packages_names()
        for pn in package_names:
            packages = self.is_available(pn)
            if packages and packages[-1].section in ('host','unit'):
                result.append(packages[-1])
        return result

    def get_outdated_host_packages(self):
        """Check and return the available host packages available and not installed"""

        logger.debug(u'Check if host package "%s" is available' % (self.host_packagename(), ))
        result = []
        host_packages = self.get_host_packages()
        for package in host_packages:
            if self.is_locally_allowed_package(package):
                logger.debug('Checking if %s is installed/outdated' % package.asrequirement())
                installed_package = self.is_installed(package.asrequirement())
                if not installed_package or installed_package < package:
                    result.append(package)
        return result

    def get_unrelevant_host_packages(self):
        """Get the implicit package names (host and unit packages) which are installed but no longer relevant

        Returns:
            list: of installed package names
        """
        installed_host_packages = [p.package for p in self.installed(True).values() if p.section in ('host','unit')]
        expected_host_packages = self.get_host_packages_names()
        return [pn for pn in installed_host_packages if pn not in expected_host_packages]

    def upgrade(self):
        """Install "well known" host package from main repository if not already installed
        then query localstatus database for packages with a version older than repository
        and install all newest packages

        Returns:
            dict: {'upgrade': [], 'additional': [], 'downloads':
                        {'downloaded': [], 'skipped': [], 'errors': []},
                     'remove': [], 'skipped': [], 'install': [], 'errors': [], 'unavailable': []}
        """
        self.runstatus='Upgrade system'
        result = dict(
            install=[],
            upgrade=[],
            additional=[],
            remove=[],
            errors=[])
        try:
            if self.use_hostpackages:
                unrelevant_host_packages = self.get_unrelevant_host_packages()
                if unrelevant_host_packages:
                    result = merge_dict(result,self.remove(unrelevant_host_packages,force=True))
                install_host_packages = self.get_outdated_host_packages()
                if install_host_packages:
                    logger.info(u'Host packages %s are available and not installed, installing host packages...' % (' '.join(h.package for h in install_host_packages),))
                    hostresult = self.install(install_host_packages,force=True)
                    result = merge_dict(result,hostresult)
                else:
                    hostresult = {}
            else:
                hostresult = {}

            upgrades = self.waptdb.upgradeable()
            logger.debug(u'upgrades : %s' % upgrades.keys())
            result = merge_dict(result,self.install(upgrades.keys(),force=True))
            self.store_upgrade_status()

            # merge results
            return merge_dict(result,hostresult)
        finally:
            self.runstatus=''

    def list_upgrade(self):
        """Returns a list of packages requirement which can be upgraded

        Returns:
           dict: {'additional': [], 'install': [], 'remove': [], 'upgrade': []}
        """
        result = dict(
            install=[],
            upgrade=[],
            additional=[],
            remove=[])
        # only most up to date (first one in list)
        # put 'host' package at the end.
        result['upgrade'].extend([p[0].asrequirement() for p in self.waptdb.upgradeable().values() if p and not p[0].section in ('host','unit')])
        if self.use_hostpackages:
            to_remove = self.get_unrelevant_host_packages()
            result['remove'].extend(to_remove)

            host_packages = self.get_outdated_host_packages()
            if host_packages:
                for p in host_packages:
                    if self.is_locally_allowed_package(p):
                        req = p.asrequirement()
                        if not req in result['install']+result['upgrade']+result['additional']:
                            result['install'].append(req)

        # get additional packages to install/upgrade based on new upgrades
        depends = self.check_depends(result['install']+result['upgrade']+result['additional'])
        for l in ('install','additional','upgrade'):
            for (r,candidate) in depends[l]:
                req = candidate.asrequirement()
                if not req in result['install']+result['upgrade']+result['additional']:
                    result[l].append(req)
        result['remove'].extend([p[1].asrequirement() for p in depends['remove'] if p[1].package not in result['remove']])
        return result

    def search(self,searchwords=[],exclude_host_repo=True,section_filter=None,newest_only=False):
        """Returns a list of packages which have the searchwords in their description

        Args:
            searchwords (str or list): words to search in packages name or description
            exclude_host_repo (boolean): if True, don't search in host repoisitories.
            section_filter (str or list): restrict search to the specified package sections/categories

        Returns:
            list: list of PackageEntry

        """
        available = self.waptdb.packages_search(searchwords=searchwords,exclude_host_repo=exclude_host_repo,section_filter=section_filter)
        installed = self.waptdb.installed(include_errors=True)
        upgradable =  self.waptdb.upgradeable()
        for p in available:
            if p.package in installed:
                current = installed[p.package]
                if p == current:
                    p['installed'] = current
                    if p.package in upgradable:
                        p['status'] = 'U'
                    else:
                        p['status'] = 'I'
                else:
                    p['installed'] = None
                    p['status'] = '-'
            else:
                p['installed'] = None
                p['status'] = '-'
        if newest_only:
            filtered = []
            last_package_name = None
            for package in sorted(available,reverse=True):
                if package.package != last_package_name:
                    filtered.append(package)
                last_package_name = package.package
            return list(reversed(filtered))
        else:
            return available

    def list(self,searchwords=[]):
        """Returns a list of installed packages which have the searchwords
        in their description

        Args:
            searchwords (list): list of words to llokup in package name and description
                                only entries which have words in the proper order are returned.

        Returns:
            list: list of PackageEntry matching the search words

        >>> w = Wapt()
        >>> w.list('zip')
        [PackageEntry('tis-7zip','16.4-8') ]
        """
        return self.waptdb.installed_search(searchwords=searchwords,)

    def check_downloads(self,apackages=None,usecache=True):
        """Return list of available package entries
        to match supplied packages requirements

        Args:
            apackages (list or str): list of packages
            usecache (bool) : returns only PackageEntry not yet in cache

        Returns:
            list: list of PackageEntry to download
        """
        result = []
        if apackages is None:
            actions = self.list_upgrade()
            apackages = actions['install']+actions['additional']+actions['upgrade']
        elif isinstance(apackages,(str,unicode)):
            apackages = ensure_list(apackages)
        elif isinstance(apackages,list):
            # ensure that apackages is a list of package requirements (strings)
            new_apackages = []
            for p in apackages:
                if isinstance(p,PackageEntry):
                    new_apackages.append(p.asrequirement())
                else:
                    new_apackages.append(p)
            apackages = new_apackages

        for p in apackages:
            entries = self.is_available(p)
            if entries:
                # download most recent
                entry = entries[-1]
                fullpackagepath = os.path.join(self.package_cache_dir,entry.filename)
                if usecache and (os.path.isfile(fullpackagepath) and os.path.getsize(fullpackagepath) == entry.size):
                    # check version
                    try:
                        cached = PackageEntry()
                        cached.load_control_from_wapt(fullpackagepath,calc_md5=False)
                        if entry != cached:
                            result.append(entry)
                    except Exception as e:
                        logger.warning('Unable to get version of cached package %s: %s'%(fullpackagepath,ensure_unicode(e),))
                        result.append(entry)
                else:
                    result.append(entry)
            else:
                logger.debug('check_downloads : Package %s is not available'%p)
        return result

    def download_upgrades(self):
        """Download packages that can be upgraded"""
        self.runstatus='Download upgrades'
        try:
            to_download = self.check_downloads()
            return self.download_packages(to_download)
        finally:
            self.runstatus=''

    def authorized_certificates(self):
        """return a list of autorized package certificate issuers for this host
            check_certificates_validity enable date checking.
        """
        return self.cabundle.certificates(valid_only = self.check_certificates_validity)

    def register_computer(self,description=None):
        """Send computer informations to WAPT Server
            if description is provided, updates local registry with new description

        Returns:
            dict: response from server.

        >>> wapt = Wapt()
        >>> s = wapt.register_computer()
        >>>

        """
        if description:
            try:
                setuphelpers.set_computer_description(description)
            except Exception as e:
                logger.critical(u'Unable to change computer description to %s: %s' % (description,e))

        # force regenerating uuid
        self.delete_param('uuid')

        inv = self.inventory()
        inv['uuid'] = self.host_uuid
        inv['host_certificate'] = self.create_or_update_host_certificate()
        data = jsondump(inv)
        if self.waptserver:
            if not self.waptserver.use_kerberos:
                urladdhost = 'add_host'
            else:
                urladdhost = 'add_host_kerberos'
            return self.waptserver.post(urladdhost,
                data = data ,
                signature = self.sign_host_content(data),
                signer = self.get_host_certificate().cn
                )
        else:
            return dict(
                success = False,
                msg = u'No WAPT server defined',
                data = data,
                )

    def get_host_key_filename(self):
        # check ACL.
        private_dir = os.path.join(self.wapt_base_dir,'private')
        if not os.path.isdir(private_dir):
            os.makedirs(private_dir)

        return os.path.join(private_dir,self.host_uuid+'.pem')


    def get_host_certificate_filename(self):
        # check ACL.
        private_dir = os.path.join(self.wapt_base_dir,'private')
        if not os.path.isdir(private_dir):
            os.makedirs(private_dir)
        return os.path.join(private_dir,self.host_uuid+'.crt')


    def get_host_certificate(self):
        """Return the current host certificate.

        Returns:
            SSLCertificate: host public certificate.
        """
        return SSLCertificate(self.get_host_certificate_filename())


    def create_or_update_host_certificate(self,force_recreate=False):
        """Create a rsa key pair for the host and a x509 certiticate.
            Location of key is <wapt_root>\private
            Should be kept secret
            restricted access to system account and administrators only.

        Args:
            force_recreate (bool): recreate key pair even if already exists for this FQDN.

        Returns:
            str: x509 certificate of this host.

        """
        key_filename = self.get_host_key_filename()
        private_dir = os.path.dirname(key_filename)
        # check ACL ?
        if not os.path.isdir(private_dir):
            os.makedirs(private_dir)

        crt_filename = self.get_host_certificate_filename()

        # clear cache
        self._host_key = None

        if force_recreate or not os.path.isfile(key_filename) or not os.path.isfile(crt_filename):
            logger.info('Creates host keys pair and x509 certificate %s' % crt_filename)

            key = SSLPrivateKey(key_filename)
            key.create()
            key.save_as_pem()

            crt = key.build_sign_certificate(None,None,
                cn = self.host_uuid,
                dnsname = setuphelpers.get_hostname(),
                organization = setuphelpers.registered_organization() or None,
                is_ca=True,
                is_code_signing=False)
            crt.save_as_pem(crt_filename)

        # check validity
        return open(crt_filename,'rb').read()

    def get_host_key(self):
        """Return private key used to sign uploaded data from host

        Returns:
            SSLPrivateKey: Private key used to sign data posted by host.
        """
        if self._host_key is None:
            # create keys pair / certificate if not yet initialised
            if not os.path.isfile(self.get_host_key_filename()):
                self.create_or_update_host_certificate()
            self._host_key = SSLPrivateKey(self.get_host_key_filename())
        return self._host_key

    def sign_host_content(self,data,md='sha256'):
        """Sign data str with host private key with sha256 + RSA
        Args:
            data (bytes) : data to sign
        Returns
            bytes: signature of sha256 hash of data.
        """
        key = self.get_host_key()
        return key.sign_content(hexdigest_for_data(str(data),md = md))

    def get_last_update_status(self):
        """Get update status of host as stored at the end of last operation.

        Returns:
            dict:
                'date': timestamp of last operation
                'runstatus': last printed message of wapt core
                'running_tasks': list of tasks
                'errors': list of packages not installed properly
                'upgrades': list of packages which need to be upgraded
        """
        status = json.loads(self.read_param('last_update_status','{"date": "", "running_tasks": [], "errors": [], "upgrades": []}'))
        status['runstatus'] = self.read_param('runstatus','')
        return json.loads(jsondump(status))

    def update_server_status(self,force=False):
        """Send host_info, installed packages and installed softwares,
            and last update status informations to WAPT Server,
            but don't send register info like dmi or wmi.

        .. versionchanged:: 1.4.3
            if last status has been properly sent to server and data has not changed,
                don't push data again to server.
            the hash is stored in memory, so is not pass across threads or processes.

        >>> wapt = Wapt()
        >>> s = wapt.update_server_status()
        >>>
        """
        def _add_data_if_updated(inv,key,data,old_hashes,new_hashes):
            """Add the data to inv as key if modified since last update_server_status"""
            newhash = hashlib.sha1(cPickle.dumps(data)).digest()
            oldhash = old_hashes.get(key,None)
            if force or oldhash != newhash:
                inv[key] = data
                new_hashes[key] = newhash

        result = None
        if self.waptserver_available():
            # avoid sending data to the server if it has not been updated.
            try:
                new_hashes = {}
                old_hashes = getattr(self,'_update_server_hashes',{})
                inv = {'uuid': self.host_uuid}
                inv['wapt_status'] = self.wapt_status()

                _add_data_if_updated(inv,'host_info',setuphelpers.host_info(),old_hashes,new_hashes)
                _add_data_if_updated(inv,'installed_softwares',setuphelpers.installed_softwares(''),old_hashes,new_hashes)
                _add_data_if_updated(inv,'installed_packages',[p.as_dict() for p in self.waptdb.installed(include_errors=True).values()],old_hashes,new_hashes)
                _add_data_if_updated(inv,'last_update_status', self.get_last_update_status(),old_hashes,new_hashes)

                data = jsondump(inv)
                signature = self.sign_host_content(data,)

                result = self.waptserver.post('update_host',
                    data = data,
                    signature = signature,
                    signer = self.get_host_certificate().cn
                    )
                if result and result['success']:
                    # stores for next round.
                    old_hashes.update(new_hashes)
                    self._update_server_hashes = old_hashes
                    self.write_param('last_update_server_status_timestamp',str(datetime.datetime.utcnow()))
                    logger.info(u'Status on server %s updated properly'%self.waptserver.server_url)
                else:
                    logger.info(u'Error updating Status on server %s: %s' % (self.waptserver.server_url,result and result['msg'] or 'No message'))

            except Exception as e:
                logger.warning(u'Unable to update server status : %s' % ensure_unicode(e))

            # force register if computer has not been registered or hostname has changed
            # this should work only if computer can authenticate on wapt server using
            # kerberos (if enabled...)
            if result and not result['success']:
                db_data = result.get('result',None)
                if not db_data or db_data.get('computer_fqdn',None) != setuphelpers.get_hostname():
                    logger.warning('Host on the server is not known or not known under this FQDN name (known as %s). Trying to register the computer...'%(db_data and db_data.get('computer_fqdn',None) or None))
                    result = self.register_computer()
                    if result and result['success']:
                        logger.warning('New registration successful')
                    else:
                        logger.critical('Unable to register: %s' % result and result['msg'])
            elif not result:
                logger.info('update_server_status failed, no result. Check server version.')
            else:
                logger.debug('update_server_status successful %s' % (result,))
        else:
            logger.info('WAPT Server is not available to store current host status')
        return result

    def waptserver_available(self):
        """Test reachability of waptserver.

        If waptserver is defined and available, return True, else False

        Returns:
            boolean: True if server is defined and actually reachable
        """
        return self.waptserver and self.waptserver.available()

    def wapt_status(self):
        """Wapt configuration and version informations

        Returns:
            dict: versions of main main files, waptservice config,
                  repos and waptserver config

        >>> w = Wapt()
        >>> w.wapt_status()
        {
        	'setuphelpers-version': '1.1.1',
        	'waptserver': {
        		'dnsdomain': u'tranquilit.local',
        		'proxies': {
        			'http': None,
        			'https': None
        		},
        		'server_url': 'https: //wapt.tranquilit.local'
        	},
        	'waptservice_protocol': 'http',
        	'repositories': [{
        		'dnsdomain': u'tranquilit.local',
        		'proxies': {
        			'http': None,
        			'https': None
        		},
        		'name': 'global',
        		'repo_url': 'http: //wapt.tranquilit.local/wapt'
        	},
        	{
        		'dnsdomain': u'tranquilit.local',
        		'proxies': {
        			'http': None,
        			'https': None
        		},
        		'name': 'wapt-host',
        		'repo_url': 'http: //srvwapt.tranquilit.local/wapt-host'
        	}],
        	'common-version': '1.1.1',
        	'wapt-exe-version': u'1.1.1.0',
        	'waptservice_port': 8088,
        	'wapt-py-version': '1.1.1'
        }
        """
        result = {}
        waptexe = os.path.join(self.wapt_base_dir,'wapt-get.exe')
        if os.path.isfile(waptexe):
            result['wapt-exe-version'] = setuphelpers.get_file_properties(waptexe)['FileVersion']
        waptservice =  os.path.join( os.path.dirname(sys.argv[0]),'waptservice.exe')
        if os.path.isfile(waptservice):
            result['waptservice-version'] = setuphelpers.get_file_properties(waptservice)['FileVersion']
        result['setuphelpers-version'] = setuphelpers.__version__
        result['wapt-py-version'] = __version__
        result['common-version'] = __version__
        result['authorized-certificates'] = [dict(crt) for crt in self.authorized_certificates()]

        # read from config
        if self.config.has_option('global','waptservice_sslport'):
            port = self.config.get('global','waptservice_sslport')
            if port:
                result['waptservice_protocol'] = 'https'
                result['waptservice_port'] = int(port)
            else:
                result['waptservice_protocol'] = None
                result['waptservice_port'] = None
        elif self.config.has_option('global','waptservice_port'):
            port = self.config.get('global','waptservice_port')
            if port:
                result['waptservice_protocol'] = 'http'
                result['waptservice_port'] = int(port)
            else:
            # could be better
                result['waptservice_protocol'] = None
                result['waptservice_port'] = None
        else:
            # could be better
            result['waptservice_protocol'] = 'http'
            result['waptservice_port'] = 8088

        result['repositories'] = [ r.as_dict() for r in self.repositories]
        if self.waptserver:
            result['waptserver'] = self.waptserver.as_dict()
        # memory usage
        current_process = psutil.Process()
        result['wapt-memory-usage'] = vars(current_process.memory_info())

        result['packages_whitelist'] = self.packages_whitelist
        result['packages_blacklist'] = self.packages_blacklist

        return result

    def reachable_ip(self):
        """Return the local IP which is most probably reachable by wapt server

        In case there are several network connections, returns the local IP
          which Windows choose for sending packets to WaptServer.

        This can be the most probable IP which would get packets from WaptServer.

        Returns:
            str: Local IP
        """
        try:
            if self.waptserver and self.waptserver.server_url:
                host = urlparse.urlparse(self.waptserver.server_url).hostname
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.settimeout(1)
                s.connect((host, 0))
                local_ip = s.getsockname()[0]
                s.close()
                return local_ip
            else:
                return None
        except:
            return None

    def inventory(self):
        """Return full inventory of the computer as a dictionary.

        Returns:
            dict: {'host_info','wapt_status','dmi','installed_softwares','installed_packages'}

        ...changed: 1.4.1: renamed keys
        """
        inv = {}
        inv['host_info'] = setuphelpers.host_info()
        try:
            inv['dmi'] = setuphelpers.dmi_info()
        except:
            inv['dmi'] = None
            logger.warning('DMI not working')

        try:
            inv['wmi'] = setuphelpers.wmi_info()
        except:
            inv['wmi'] = None
            logger.warning('WMI unavailable')

        inv['wapt_status'] = self.wapt_status()
        inv['installed_softwares'] = setuphelpers.installed_softwares('')
        inv['installed_packages'] = [p.as_dict() for p in self.waptdb.installed(include_errors=True).values()]
        """
        try:
            inv['qfe'] = setuphelpers.installed_windows_updates()
        except:
            pass
        """
        return inv

    def personal_certificate(self):
        cert_chain = SSLCABundle()
        cert_chain.add_pem(pem_filename = self.personal_certificate_path)
        return cert_chain.certificates()

    def private_key(self,passwd_callback=None,private_key_password = None):
        """SSLPrivateKey matching the personal_certificate
        When key has been found, it is kept in memory for later use.

        Args:
            passwd_callback : func to call to get a password from user (must return str when called)
            private_key_password : password to use to decrypt key. If None, passwd_callback is called.

        Returns:
            SSLPrivateKey

        Raises:
            EWaptMissingPrivateKey if ket can not be decrypted or found.
        """
        if passwd_callback is None and private_key_password is None:
            passwd_callback = default_pwd_callback

        certs = self.personal_certificate()
        cert = certs[0]
        if not self._private_key_cache or not cert.match_key(self._private_key_cache):
            self._private_key_cache = cert.matching_key_in_dirs(password_callback=passwd_callback,private_key_password=private_key_password)
        if self._private_key_cache is None:
            raise EWaptMissingPrivateKey(u'The key matching the certificate %s can not be found or decrypted' % (cert.public_cert_filename or cert.subject))
        return self._private_key_cache

    def sign_package(self,zip_or_directoryname,certificate=None,callback=None,private_key_password=None):
        """Calc the signature of the WAPT/manifest.sha256 file and put/replace it in ZIP or directory.
            if directory, creates WAPT/manifest.sha256 and add it to the content of package
            create a WAPT/signature file and it to directory or zip file.

            known issue : if zip file already contains a manifest.sha256 file, it is not removed, so there will be
                          2 manifest files in zip / wapt package.

        Args:
            zip_or_directoryname: filename or path for the wapt package's content
            certificate: path to the certificate of signer.
            callback: ref to the function to call if a password is required for opening the private key.

        Returns:
            str: base64 encoded signature of manifest.sha256 file (content
        """
        if not isinstance(zip_or_directoryname,unicode):
            zip_or_directoryname = unicode(zip_or_directoryname)
        if certificate is None:
            certificate = self.personal_certificate()

        if isinstance(certificate,list):
            signer_cert = certificate[0]
        else:
            signer_cert = certificate
        key = signer_cert.matching_key_in_dirs(password_callback=callback,private_key_password=private_key_password)

        logger.info(u'Using identity : %s' % signer_cert.cn)
        pe =  PackageEntry().load_control_from_wapt(zip_or_directoryname)
        return pe.sign_package(private_key=key,certificate = certificate,password_callback=callback,private_key_password=private_key_password,mds = self.sign_digests)

    def build_package(self,directoryname,inc_package_release=False,excludes=['.svn','.git','.gitignore','setup.pyc'],
                target_directory=None,set_maturity=None):
        """Build the WAPT package from a directory

        Call update_control from setup.py if this function is defined.
        Then zip the content of directory. Add a manifest.sha256 file with sha256 hash of
        the content of each file.

        Args:
            directoryname (str): source root directory of package to build
            inc_package_release (boolean): increment the version of package in control file.
            set_maturity (str): if not None, change package maturity to this. Can be something like DEV, PROD etc..

        Returns:
            str: Filename of built WAPT package
        """
        if not isinstance(directoryname,unicode):
            directoryname = unicode(directoryname)
        result_filename = u''
        # some checks
        if not os.path.isdir(os.path.join(directoryname,'WAPT')):
            raise EWaptNotAPackage('Error building package : There is no WAPT directory in %s' % directoryname)
        if not os.path.isfile(os.path.join(directoryname,'WAPT','control')):
            raise EWaptNotAPackage('Error building package : There is no control file in WAPT directory')

        control_filename = os.path.join(directoryname,'WAPT','control')
        force_utf8_no_bom(control_filename)

        logger.info(u'Load control informations from control file')
        entry = PackageEntry(waptfile = directoryname)
        if set_maturity is not None:
            entry.maturity = set_maturity

        # increment inconditionally the package buuld nr.
        if inc_package_release:
            entry.inc_build()

        entry.save_control_to_wapt()

        result_filename = entry.build_package(excludes = excludes,target_directory = target_directory)
        return result_filename


    def build_upload(self,sources_directories,private_key_passwd=None,wapt_server_user=None,wapt_server_passwd=None,inc_package_release=False,
        target_directory=None,set_maturity=None):
        """Build a list of packages and upload the resulting packages to the main repository.
        if section of package is group or host, user specific wapt-host or wapt-group

        Returns
            list: list of filenames of built WAPT package
        """
        sources_directories = ensure_list(sources_directories)
        buildresults = []

        if not self.personal_certificate_path or not os.path.isfile(self.personal_certificate_path):
            raise EWaptMissingPrivateKey('Unable to build %s, personal certificate path %s not provided or not present'%(sources_directories,self.personal_certificate_path))

        for source_dir in [os.path.abspath(p) for p in sources_directories]:
            if os.path.isdir(source_dir):
                logger.info(u'Building  %s' % source_dir)
                package_fn = self.build_package(source_dir,inc_package_release=inc_package_release,target_directory=target_directory,set_maturity=set_maturity)
                if package_fn:
                    logger.info(u'...done. Package filename %s' % (package_fn,))
                    logger.info('Signing %s with certificate %s' % (package_fn,self.personal_certificate() ))
                    signature = self.sign_package(package_fn,private_key_password = private_key_passwd)
                    logger.debug(u"Package %s signed : signature :\n%s" % (package_fn,signature))
                    buildresults.append(package_fn)
                else:
                    logger.critical(u'package %s not created' % package_fn)
            else:
                logger.critical(u'Directory %s not found' % source_dir)

        logger.info(u'Uploading %s files...' % len(buildresults))
        auth = None
        if wapt_server_user and wapt_server_passwd:
            auth = (wapt_server_user,wapt_server_passwd)
        upload_res = self.waptserver.upload_packages(buildresults,auth=auth)
        if buildresults and not upload_res:
            raise Exception('Packages built but no package were uploaded')
        return buildresults

    def cleanup_session_setup(self):
        """Remove all current user session_setup informations for removed packages
        """
        installed = self.installed(False)
        self.waptsessiondb.remove_obsolete_install_status(installed.keys())

    def session_setup(self,packagename,force=False):
        """Setup the user session for a specific system wide installed package"
           Source setup.py from database or filename
        """
        install_id = None
        old_stdout = sys.stdout
        old_stderr = sys.stderr

        logger.info(u"Session setup for package %s and user %s" % (packagename,self.user))

        oldpath = sys.path

        if os.path.isdir(packagename):
            package_entry = PackageEntry().load_control_from_wapt(packagename)
        else:
            package_entry = self.is_installed(packagename)

        if not package_entry:
            raise Exception('Package %s is not installed' % packagename)

        # initialize a session db for the user
        session_db =  WaptSessionDB(self.user)  # WaptSessionDB()
        with session_db:
            if force or os.path.isdir(packagename) or not session_db.is_installed(package_entry.package,package_entry.version):
                try:
                    previous_cwd = os.getcwdu()

                    # source setup.py to get session_setup func
                    if os.path.isdir(packagename):
                        package_fn = os.path.join(packagename,'setup.py')
                        setup = import_setup(package_fn)
                        logger.debug(u'Source import OK from %s' % package_fn)
                    else:
                        logger.debug(u'Sourcing setup from DB (only if session_setup found)')
                        setuppy = package_entry['setuppy']
                        if setuppy and 'session_setup()' in setuppy:
                            setup = import_code(setuppy)
                            logger.debug(u'Source setup.py import OK from database')
                        else:
                            setup = None

                    required_params = []

                     # be sure some minimal functions are available in setup module at install step
                    if setup and hasattr(setup,'session_setup'):
                        logger.info(u'Launch session_setup')
                        # initialize a session record for this package
                        install_id = session_db.add_start_install(package_entry.package,package_entry.version,package_entry.architecture)

                        # redirect output to get print into session db log
                        sys.stderr = sys.stdout = install_output = LogInstallOutput(sys.stderr,session_db,install_id)
                        try:
                            setattr(setup,'run',self.run)
                            setattr(setup,'run_notfatal',self.run_notfatal)
                            setattr(setup,'user',self.user)
                            setattr(setup,'usergroups',self.usergroups)
                            setattr(setup,'control',package_entry)
                            setattr(setup,'WAPT',self)
                            setattr(setup,'language',self.language)

                            # get definitions of required parameters from setup module
                            if hasattr(setup,'required_params'):
                                required_params = setup.required_params

                            # get value of required parameters from system wide install
                            try:
                                params_dict=json.loads(self.waptdb.query("select install_params from wapt_localstatus where package=?",[package_entry.package,])[0]['install_params'])
                            except:
                                logger.warning(u'Unable to get installation parameters from wapt database for package %s' % package_entry.package)
                                params_dict={}

                            # set params dictionary
                            if not hasattr(setup,'params'):
                                # create a params variable for the setup module
                                setattr(setup,'params',params_dict)
                            else:
                                # update the already created params with additional params from command line
                                setup.params.update(params_dict)

                            session_db.update_install_status(install_id,'RUNNING','Launch session_setup()\n')
                            result = setup.session_setup()
                            if result:
                                session_db.update_install_status(install_id,'RETRY','session_setup() done\n')
                            else:
                                session_db.update_install_status(install_id,'OK','session_setup() done\n')
                            return result

                        except Exception as e:
                            if install_id:
                                try:
                                    try:
                                        uerror = repr(e).decode(locale.getpreferredencoding())
                                    except:
                                        uerror = ensure_unicode(e)
                                    session_db.update_install_status(install_id,'ERROR',uerror)
                                except Exception as e2:
                                    logger.critical(ensure_unicode(e2))
                            else:
                                logger.critical(ensure_unicode(e))
                            raise e
                        finally:
                            # restore normal output
                            sys.stdout = old_stdout
                            sys.stderr = old_stderr
                            sys.path = oldpath

                    else:
                        print('No session-setup.')
                finally:
                    # cleanup
                    if 'setup' in dir() and setup is not None:
                        setup_name = setup.__name__
                        logger.debug('Removing module %s'%setup_name)
                        del setup
                        if setup_name in sys.modules:
                            del sys.modules[setup_name]
                    sys.path = oldpath
                    logger.debug(u'  Change current directory to %s.' % previous_cwd)
                    os.chdir(previous_cwd)
            else:
                print('Already installed.')

    def uninstall(self,packagename,params_dict={}):
        """Launch the uninstall script of an installed package"
        Source setup.py from database or filename
        """
        logger.info(u"setup.uninstall for package %s with params %s" % (packagename,params_dict))
        oldpath = sys.path
        try:
            setup = None
            previous_cwd = os.getcwdu()
            if os.path.isdir(packagename):
                entry = PackageEntry().load_control_from_wapt(packagename)
                setup = import_setup(os.path.join(packagename,'setup.py'))
            else:
                logger.debug(u'Sourcing setup from DB')
                entry = self.is_installed(packagename)
                if entry['setuppy'] is not None:
                    setup = import_code(entry['setuppy'])

            if setup:
                required_params = []
                 # be sure some minimal functions are available in setup module at install step
                logger.debug(u'Source import OK')
                if hasattr(setup,'uninstall'):
                    logger.info(u'Launch uninstall')
                    setattr(setup,'run',self.run)
                    setattr(setup,'run_notfatal',self.run_notfatal)
                    setattr(setup,'user',self.user)
                    setattr(setup,'usergroups',self.usergroups)
                    setattr(setup,'control',entry)
                    setattr(setup,'WAPT',self)
                    setattr(setup,'language',self.language)

                    # get value of required parameters if not already supplied
                    for p in required_params:
                        if not p in params_dict:
                            if not is_system_user():
                                params_dict[p] = raw_input("%s: " % p)
                            else:
                                raise Exception(u'Required parameters %s is not supplied' % p)

                    # set params dictionary
                    if not hasattr(setup,'params'):
                        # create a params variable for the setup module
                        setattr(setup,'params',params_dict)
                    else:
                        # update the already created params with additional params from command line
                        setup.params.update(params_dict)

                    result = setup.uninstall()
                    return result
                else:
                    logger.debug(u'No uninstall() function in setup.py for package %s' % packagename)
                    #raise Exception(u'No uninstall() function in setup.py for package %s' % packagename)
            else:
                logger.info('Uninstall: no setup.py source in database.')

        finally:
            if 'setup' in dir() and setup is not None:
                setup_name = setup.__name__
                del setup
                if setup_name in sys.modules:
                    del sys.modules[setup_name]

            sys.path = oldpath
            logger.debug(u'  Change current directory to %s' % previous_cwd)
            os.chdir(previous_cwd)

    def make_package_template(self,installer_path='',packagename='',directoryname='',section='',description=None,depends='',version=None,silentflags=None,uninstallkey=None):
        r"""Build a skeleton of WAPT package based on the properties of the supplied installer
           Return the path of the skeleton
        >>> wapt = Wapt(config_filename='c:/wapt/wapt-get.ini')
        >>> wapt.dbpath = ':memory:'
        >>> files = 'c:/tmp/files'
        >>> if not os.path.isdir(files):
        ...    os.makedirs(files)
        >>> tmpdir = 'c:/tmp/dummy'
        >>> devdir = wapt.make_package_template(files,packagename='mydummy',directoryname=tmpdir,depends='tis-firefox')
        >>> os.path.isfile(os.path.join(devdir,'WAPT','control'))
        True
        >>> p = wapt.build_package(devdir)
        >>> 'filename' in p and isinstance(p['files'],list) and isinstance(p['package'],PackageEntry)
        True
        >>> import shutil
        >>> shutil.rmtree(tmpdir)
        """
        if installer_path:
            installer_path = os.path.abspath(installer_path)
        if directoryname:
             directoryname = os.path.abspath(directoryname)

        if not installer_path and not packagename:
            raise EWaptException('You must provide at least installer_path or packagename to be able to prepare a package template')

        if installer_path:
            installer = os.path.basename(installer_path)
        else:
            installer = ''

        uninstallkey = uninstallkey or  ''

        if os.path.isfile(installer_path):
            # case of an installer
            props = setuphelpers.getproductprops(installer_path)
            silentflags = silentflags or setuphelpers.getsilentflags(installer_path)
            # for MSI, uninstallkey is in properties
            if not uninstallkey and 'ProductCode' in props:
                uninstallkey = '"%s"' % props['ProductCode']
        elif os.path.isdir(installer_path):
            # case of a directory
            props = {
                'product':installer,
                'description':installer,
                'version': '0',
                'publisher':ensure_unicode(setuphelpers.get_current_user())
                }
            silentflags = silentflags or ''
        else:
            # case of a nothing
            props = {
                'product':packagename,
                'description':packagename,
                'version': '0',
                'publisher':ensure_unicode(setuphelpers.get_current_user())
                }
            silentflags = ''

        if not packagename:
            simplename = re.sub(r'[\s\(\)]+','',props['product'].lower())
            packagename = '%s-%s' %  (self.config.get('global','default_package_prefix'),simplename)

        description = description or 'Package for %s ' % props['description']
        version = version or props['version']

        if not directoryname:
            directoryname = self.get_default_development_dir(packagename,section='base')

        if not os.path.isdir(os.path.join(directoryname,'WAPT')):
            os.makedirs(os.path.join(directoryname,'WAPT'))

        if installer_path:
            (installer_name,installer_ext) = os.path.splitext(installer)
            if installer_ext == '.msi':
                setup_template = os.path.join(self.wapt_base_dir,'templates','setup_package_template_msi.py')
            elif installer_ext == '.msu':
                setup_template = os.path.join(self.wapt_base_dir,'templates','setup_package_template_msu.py')
            elif installer_ext == '.exe':
                setup_template = os.path.join(self.wapt_base_dir,'templates','setup_package_template_exe.py')
            else:
                setup_template = os.path.join(self.wapt_base_dir,'templates','setup_package_template.py')
        else:
            setup_template = os.path.join(self.wapt_base_dir,'templates','setup_package_skel.py')

        template = codecs.open(setup_template,encoding='utf8').read()%dict(
            packagename=packagename,
            uninstallkey=uninstallkey,
            silentflags=silentflags,
            installer = installer,
            product=props['product'],
            description=description,
            version=version,
            )
        setuppy_filename = os.path.join(directoryname,'setup.py')
        if not os.path.isfile(setuppy_filename):
            codecs.open(setuppy_filename,'w',encoding='utf8').write(template)
        else:
            logger.info(u'setup.py file already exists, skip create')
        logger.debug(u'Copy installer %s to target' % installer)
        if os.path.isfile(installer_path):
            shutil.copyfile(installer_path,os.path.join(directoryname,installer))
        elif os.path.isdir(installer_path):
            setuphelpers.copytree2(installer_path,os.path.join(directoryname,installer))

        control_filename = os.path.join(directoryname,'WAPT','control')
        if not os.path.isfile(control_filename):
            entry = PackageEntry()
            entry.package = packagename
            entry.architecture='all'
            entry.description = description
            try:
                entry.maintainer = ensure_unicode(win32api.GetUserNameEx(3))
            except:
                try:
                    entry.maintainer = ensure_unicode(setuphelpers.get_current_user())
                except:
                    entry.maintainer = os.environ['USERNAME']

            entry.priority = 'optional'
            entry.section = section or 'base'
            entry.version = version+'-0'
            entry.depends = depends
            if self.config.has_option('global','default_sources_url'):
                entry.sources = self.config.get('global','default_sources_url') % {'packagename':packagename}
            codecs.open(control_filename,'w',encoding='utf8').write(entry.ascontrol())
        else:
            logger.info(u'control file already exists, skip create')

        self.add_pyscripter_project(directoryname)

        return directoryname

    def make_host_template(self,packagename='',depends=None,conflicts=None,directoryname=None,description=None):
        if not packagename:
            packagename = self.host_packagename()
        return self.make_group_template(packagename=packagename,depends=depends,conflicts=conflicts,directoryname=directoryname,section='host',description=description)

    def make_group_template(self,packagename='',depends=None,conflicts=None,directoryname=None,section='group',description=None):
        r"""Creates or updates on disk a skeleton of a WAPT group package.
        If the a package skeleton already exists in directoryname, it is updated.

        sourcespath attribute of returned PackageEntry is populated with the developement directory of group package.

        Args:
            packagename (str): group name
            depends :
            conflicts
            directoryname
            section
            description

        Returns:
            PackageEntry

        >>> wapt = Wapt(config_filename='c:/wapt/wapt-get.ini')
        >>> tmpdir = 'c:/tmp/dummy'
        >>> if os.path.isdir(tmpdir):
        ...    import shutil
        ...    shutil.rmtree(tmpdir)
        >>> p = wapt.make_group_template(packagename='testgroupe',directoryname=tmpdir,depends='tis-firefox',description=u'Test de groupe')
        >>> print p
        >>> print p['package'].depends
        tis-firefox
        >>> import shutil
        >>> shutil.rmtree(tmpdir)
        """
        if directoryname:
             directoryname = os.path.abspath(directoryname)

        if not packagename:
            packagename = self.host_packagename()

        if not directoryname:
            directoryname = self.get_default_development_dir(packagename,section=section)

        if not directoryname:
            directoryname = tempfile.mkdtemp('wapt')

        if not os.path.isdir(os.path.join(directoryname,'WAPT')):
            os.makedirs(os.path.join(directoryname,'WAPT'))

        template_fn = os.path.join(self.wapt_base_dir,'templates','setup_%s_template.py' % section)
        if os.path.isfile(template_fn):
            # replacing %(var)s by local values in template
            # so setup template must use other string formating system than % like '{}'.format()
            template = codecs.open(template_fn,encoding='utf8').read() % locals()
            setuppy_filename = os.path.join(directoryname,'setup.py')
            if not os.path.isfile(setuppy_filename):
                codecs.open(setuppy_filename,'w',encoding='utf8').write(template)
            else:
                logger.info(u'setup.py file already exists, skip create')
        else:
            logger.info(u'No %s template. Package wil lhave no setup.py' % template_fn)

        control_filename = os.path.join(directoryname,'WAPT','control')
        entry = PackageEntry()
        if not os.path.isfile(control_filename):
            entry.priority = 'standard'
            entry.section = section
            entry.version = '0'
            entry.architecture='all'
            entry.description = description or u'%s package for %s ' % (section,packagename)
            try:
                entry.maintainer = ensure_unicode(win32api.GetUserNameEx(3))
            except:
                try:
                    entry.maintainer = ensure_unicode(setuphelpers.get_current_user())
                except:
                    entry.maintainer = os.environ['USERNAME']
        else:
            entry.load_control_from_wapt(directoryname)

        entry.package = packagename

        # Check existing versions and increment it
        older_packages = self.is_available(entry.package)
        if older_packages and entry<=older_packages[-1]:
            entry.version = older_packages[-1].version
            entry.inc_build()

        entry.filename = entry.make_package_filename()

        if self.config.has_option('global','default_sources_url'):
            entry.sources = self.config.get('global','default_sources_url') % {'packagename':packagename}

        # check if depends should be appended to existing depends
        if (isinstance(depends,str) or isinstance(depends,unicode)) and depends.startswith('+'):
            append_depends = True
            depends = ensure_list(depends[1:])
            current = ensure_list(entry.depends)
            for d in depends:
                if not d in current:
                    current.append(d)
            depends = current
        else:
            append_depends = False

        depends = ensure_list(depends)
        if depends:
            # use supplied list of packages
            entry.depends = ','.join([u'%s' % p for p in depends if p and p != packagename ])


        # check if conflicts should be appended to existing conflicts
        if (isinstance(conflicts,str) or isinstance(conflicts,unicode)) and conflicts.startswith('+'):
            append_conflicts = True
            conflicts = ensure_list(conflicts[1:])
            current = ensure_list(entry.conflicts)
            for d in conflicts:
                if not d in current:
                    current.append(d)
            conflicts = current
        else:
            append_conflicts = False

        conflicts = ensure_list(conflicts)
        if conflicts:
            # use supplied list of packages
            entry.conflicts = ','.join([u'%s' % p for p in conflicts if p and p != packagename ])

        entry.save_control_to_wapt(directoryname)
        if entry.section != 'host':
            self.add_pyscripter_project(directoryname)
        return entry

    def is_installed(self,packagename,include_errors=False):
        """Checks if a package is installed.
        Return package entry and additional local status or None

        Args:
            packagename (str): name / package request to query

        Returns:
            PackageEntry: None en PackageEntry merged with local install_xxx fields
                          * install_date
                          * install_output
                          * install_params
                          * install_status
        """
        if isinstance(packagename,PackageEntry):
            packagename = packagename.asrequirement()
        return self.waptdb.installed_matching(packagename,include_errors=include_errors)

    def installed(self,include_errors=False):
        """Returns all installed packages with their status

        Args:
            include_errors (boolean): include packages wnot installed successfully

        Returns:
            list: list of PackageEntry merged with local install status.
        """
        return self.waptdb.installed(include_errors=include_errors)

    def is_available(self,packagename):
        r"""Check if a package (with optional version condition) is available
        in repositories.

        Args:
            packagename (str) : package name to lookup or package requirement ( packagename(=version) )

        Returns:
            list : of PackageEntry sorted by package version ascending

        >>> wapt = Wapt(config_filename='c:/tranquilit/wapt/tests/wapt-get.ini')
        >>> l = wapt.is_available('tis-wapttest')
        >>> l and isinstance(l[0],PackageEntry)
        True
        """
        return self.waptdb.packages_matching(packagename)

    def get_default_development_dir(self,packagecond,section='base'):
        """Returns the default development directory for package named <packagecond>
        based on default_sources_root ini parameter if provided

        Args:
            packagecond (PackageEntry or str): either PackageEntry or a "name(=version)" string

        Returns:
            unicode: path to local proposed development directory
        """
        if not isinstance(packagecond,PackageEntry):
            # assume something like "package(=version)"
            package_and_version = REGEX_PACKAGE_CONDITION.match(packagecond).groupdict()
            pe = PackageEntry(package_and_version['package'],package_and_version['version'] or '0')
        else:
            pe = packagecond

        root = ensure_unicode(self.config.get('global','default_sources_root'))
        if not root:
            root = ensure_unicode(tempfile.gettempdir())
        return os.path.join(root, pe.make_package_edit_directory())

    def add_pyscripter_project(self,target_directory):
        """Add a pyscripter project file to package development directory.

        Args:
            target_directory (str): path to location where to create the wa^t.psproj file.

        Returns:
            None
        """
        psproj_filename = os.path.join(target_directory,'WAPT','wapt.psproj')
        #if not os.path.isfile(psproj_filename):
        # supply some variables to psproj template
        datas = self.as_dict()
        datas['target_directory'] = target_directory
        proj_template = codecs.open(os.path.join(self.wapt_base_dir,'templates','wapt.psproj'),encoding='utf8').read()%datas
        codecs.open(psproj_filename,'w',encoding='utf8').write(proj_template)

    def edit_package(self,packagerequest,
            target_directory='',
            use_local_sources=True,
            append_depends=None,
            remove_depends=None,
            append_conflicts=None,
            remove_conflicts=None,
            auto_inc_version=True,
            cabundle=None,
            ):
        r"""Download an existing package from repositories into target_directory for modification
        if use_local_sources is True and no newer package exists on repos, updates current local edited data
        else if target_directory exists and is not empty, raise an exception

        Args:
            packagerequest (str) : path to existing wapt file, or package request
            use_local_sources (boolean) : don't raise an exception if target exist and match package version
            append_depends (list of str) : package requirements to add to depends
            remove_depends (list or str) : package requirements to remove from depends
            auto_inc_version (bool) :
            cabundle  (SSLCABundle) : list of authorized certificate filenames. If None, use default from current wapt.

        Returns:
            PackageEntry : edit local package with sourcespath attribute populated

        >>> wapt = Wapt(config_filename='c:/tranquilit/wapt/tests/wapt-get.ini')
        >>> wapt.dbpath = ':memory:'
        >>> r= wapt.update()
        >>> tmpdir = tempfile.mkdtemp('wapt')
        >>> res = wapt.edit_package('tis-wapttest',target_directory=tmpdir,append_depends='tis-firefox',remove_depends='tis-7zip')
        >>> res['target'] == tmpdir and res['package'].package == 'tis-wapttest' and 'tis-firefox' in res['package'].depends
        True
        >>> import shutil
        >>> shutil.rmtree(tmpdir)

        """
        if cabundle is None:
            cabundle = self.cabundle

        # check if available in repos
        entries = self.is_available(packagerequest)
        if entries:
            entry = entries[-1]
            self.download_packages(entry)
        elif os.path.isfile(packagerequest):
            # argument is a wapt package filename, replace packagerequest with entry
            entry = PackageEntry(waptfile=packagerequest)
        else:
            raise EWaptException(u'Package %s does not exist. Either update local status or check filepath.' % (packagerequest))

        packagerequest = entry.asrequirement()

        if target_directory is None:
            target_directory = tempfile.mkdtemp(prefix="wapt")
        elif not target_directory:
            target_directory = self.get_default_development_dir(entry.package,section=entry.section)

        if entry.localpath:
            local_dev_entry = self.is_wapt_package_development_dir(target_directory)
            if local_dev_entry:
                if use_local_sources and not local_dev_entry.match(packagerequest):
                    raise Exception('Target directory %s contains a different package version %s' % (target_directory,entry.asrequirement()))
                elif not use_local_sources:
                    raise Exception('Target directory %s contains already a developement package %s' % (target_directory,entry.asrequirement()))
                else:
                    logger.info('Using existing development sources %s' % target_directory)
            elif not local_dev_entry:
                entry.unzip_package(target_dir=target_directory, cabundle = cabundle)
                entry.invalidate_signature()
                local_dev_entry = entry

            append_depends = ensure_list(append_depends)
            remove_depends = ensure_list(remove_depends)
            append_conflicts = ensure_list(append_conflicts)
            remove_conflicts = ensure_list(remove_conflicts)

            if append_depends or remove_depends or append_conflicts or remove_conflicts:
                prev_depends = ensure_list(local_dev_entry.depends)
                for d in append_depends:
                    if not d in prev_depends:
                        prev_depends.append(d)

                for d in remove_depends:
                    if d in prev_depends:
                        prev_depends.remove(d)

                prev_conflicts = ensure_list(local_dev_entry.conflicts)
                for d in append_conflicts:
                    if not d in prev_conflicts:
                        prev_conflicts.append(d)

                if remove_conflicts:
                    for d in remove_conflicts:
                        if d in prev_conflicts:
                            prev_conflicts.remove(d)


                local_dev_entry.depends = ','.join(prev_depends)
                local_dev_entry.conflicts = ','.join(prev_conflicts)
                local_dev_entry.save_control_to_wapt(target_directory)

            if entry.section != 'host':
                self.add_pyscripter_project(target_directory)
            return local_dev_entry
        else:
            raise Exception(u'Unable to unzip package in %s' % target_directory)

    def is_wapt_package_development_dir(self,directory):
        """Return PackageEntry if directory is a wapt developement directory (a WAPT/control file exists) or False"""
        return os.path.isfile(os.path.join(directory,'WAPT','control')) and PackageEntry().load_control_from_wapt(directory,calc_md5=False)

    def is_wapt_package_file(self,filename):
        """Return PackageEntry if filename is a wapt package or False"""
        (root,ext)=os.path.splitext(filename)
        if ext != '.wapt' or not os.path.isfile(filename):
            return False
        try:
            entry = PackageEntry().load_control_from_wapt(filename,calc_md5=False)
            return entry
        except:
            return False

    def edit_host(self,
            hostname,
            target_directory=None,
            append_depends=None,
            remove_depends=None,
            append_conflicts=None,
            remove_conflicts=None,
            printhook=None,
            description=None,
            cabundle=None,
            ):
        """Download and extract a host package from host repositories into target_directory for modification

        Args:
            hostname       (str)   : fqdn of the host to edit
            target_directory (str)  : where to place the developments files. if empty, use default one from wapt-get.ini configuration
            append_depends (str or list) : list or comma separated list of package requirements
            remove_depends (str or list) : list or comma separated list of package requirements to remove
            cabundle (SSLCA Bundle) : authorized ca certificates. If None, use default from current wapt.

        Returns:
            PackageEntry

        >>> wapt = Wapt(config_filename='c:/wapt/wapt-get.ini')
        >>> tmpdir = 'c:/tmp/dummy'
        >>> wapt.edit_host('dummy.tranquilit.local',target_directory=tmpdir,append_depends='tis-firefox')
        >>> import shutil
        >>> shutil.rmtree(tmpdir)
        >>> host = wapt.edit_host('htlaptop.tranquilit.local',target_directory=tmpdir,append_depends='tis-firefox')
        >>> 'package' in host
        True
        >>> shutil.rmtree(tmpdir)
        """
        if target_directory is None:
            target_directory = tempfile.mkdtemp('wapt')
        elif not target_directory:
            target_directory = self.get_default_development_dir(hostname,section='host')

        if os.path.isdir(target_directory) and os.listdir(target_directory):
            raise Exception('directory %s is not empty, aborting.' % target_directory)

        #self.use_hostpackages = True

        if cabundle is None:
            cabundle = self.cabundle

        append_depends = ensure_list(append_depends)
        remove_depends = ensure_list(remove_depends)
        append_conflicts = ensure_list(append_conflicts)
        remove_conflicts = ensure_list(remove_conflicts)

        for d in append_depends:
            if not d in remove_conflicts:
                remove_conflicts.append(d)

        for d in append_conflicts:
            if not d in remove_depends:
                remove_depends.append(d)

        # create a temporary repo for this host
        host_repo = WaptHostRepo(name='wapt-host',host_id=hostname,config = self.config,host_key = self._host_key)
        entry = host_repo.get(hostname)
        if entry:
            host_repo.download_packages(entry)
            entry.unzip_package(target_dir=target_directory,cabundle=cabundle)
            entry.invalidate_signature()

            # update depends list
            prev_depends = ensure_list(entry.depends)
            for d in append_depends:
                if not d in prev_depends:
                    prev_depends.append(d)
            for d in remove_depends:
                if d in prev_depends:
                    prev_depends.remove(d)
            entry.depends = ','.join(prev_depends)

            # update conflicts list
            prev_conflicts = ensure_list(entry.conflicts)
            for d in append_conflicts:
                if not d in prev_conflicts:
                    prev_conflicts.append(d)
            if remove_conflicts:
                for d in remove_conflicts:
                    if d in prev_conflicts:
                        prev_conflicts.remove(d)
            entry.conflicts = ','.join(prev_conflicts)
            if description is not None:
                entry.description = description

            entry.save_control_to_wapt(target_directory)
            return entry
        else:
            # create a new version of the existing package in repository
            return self.make_host_template(packagename=hostname,directoryname=target_directory,depends=append_depends,description=description)

    def forget_packages(self,packages_list):
        """Remove install status for packages from local database
        without actually uninstalling the packages

        Args:
            packages_list (list): list of installed package names to forget

        Returns:
            list: list of package names actually forgotten

        >>> wapt = Wapt(config_filename='c:/wapt/wapt-get.ini')
        >>> res = wapt.install('tis-test')
        ???
        >>> res = wapt.is_installed('tis-test')
        >>> isinstance(res,PackageEntry)
        True
        >>> wapt.forget_packages('tis-test')
        ['tis-test']
        >>> wapt.is_installed('tis-test')
        >>> print wapt.is_installed('tis-test')
        None
        """
        result = []
        packages_list = ensure_list(packages_list)
        for package in packages_list:
            rowid = self.waptdb.remove_install_status(package)
            if rowid:
                result.append(package)
        return result

    def duplicate_package(self,
            packagename,
            newname=None,
            newversion=None,
            target_directory=None,
            append_depends=None,
            remove_depends=None,
            append_conflicts=None,
            remove_conflicts=None,
            auto_inc_version=True,
            usecache=True,
            printhook=None,
            cabundle = None,
            ):
        """Duplicate an existing package.
        Duplicate an existing package from declared repostory or file into targetdirectory with
          optional newname and version.

        Args:
            packagename (str) :      packagename to duplicate, or filepath to a local package or package development directory.
            newname (str):           name of target package
            newversion (str):        version of target package. if None, use source package version
            target_directory (str):  path where to put development files. If None, use temporary. If empty, use default development dir
            append_depends (list):   comma str or list of depends to append.
            remove_depends (list):   comma str or list of depends to remove.
            auto_inc_version (bool): if version is less than existing package in repo, set version to repo version+1
            usecache (bool):         If True, allow to use cached package in local repo instead of downloading it.
            printhook (func):        hook for download progress
            cabundle (SSLCABundle):         list of authorized ca certificate (SSLPublicCertificate) to check authenticity of source packages. If None, no check is performed.

        Returns:
            PackageEntry : new packageEntry with sourcespath = target_directory

        >>> wapt = Wapt(config_filename='c:/tranquilit/wapt/tests/wapt-get.ini')
        >>> wapt.dbpath = ':memory:'
        >>> r= wapt.update()
        >>> def nullhook(*args):
        ...     pass
        >>> tmpdir = 'c:/tmp/testdup-wapt'
        >>> if os.path.isdir(tmpdir):
        ...     import shutil
        ...     shutil.rmtree(tmpdir)
        >>> p = wapt.duplicate_package('tis-wapttest',
        ...     newname='testdup',
        ...     newversion='20.0-0',
        ...     target_directory=tmpdir,
        ...     excludes=['.svn','.git','.gitignore','*.pyc','src'],
        ...     append_depends=None,
        ...     auto_inc_version=True,
        ...     usecache=False,
        ...     printhook=nullhook)
        >>> print repr(p['package'])
        PackageEntry('testdup','20.0-0')
        >>> if os.path.isdir(tmpdir):
        ...     import shutil
        ...     shutil.rmtree(tmpdir)
        >>> p = wapt.duplicate_package('tis-wapttest',
        ...    target_directory=tempfile.mkdtemp('wapt'),
        ...    auto_inc_version=True,
        ...    append_depends=['tis-firefox','tis-irfanview'],
        ...    remove_depends=['tis-wapttestsub'],
        ...    )
        >>> print repr(p['package'])
        PackageEntry('tis-wapttest','120')
        """
        if target_directory:
             target_directory = os.path.abspath(target_directory)

        if newname:
            while newname.endswith('.wapt'):
                dot_wapt = newname.rfind('.wapt')
                newname = newname[0:dot_wapt]
                logger.warning("Target ends with '.wapt', stripping.  New name: %s", newname)

        # default empty result
        result = {}

        append_depends = ensure_list(append_depends)
        remove_depends = ensure_list(remove_depends)
        append_conflicts = ensure_list(append_conflicts)
        remove_conflicts = ensure_list(remove_conflicts)

        def check_target_directory(target_directory,source_control):
            if os.path.isdir(target_directory) and os.listdir(target_directory):
                pe = PackageEntry().load_control_from_wapt(target_directory)
                if  pe.package != source_control.package or pe > source_control:
                    raise Exception('Target directory "%s" is not empty and contains either another package or a newer version, aborting.' % target_directory)

        # duplicate a development directory tree
        if os.path.isdir(packagename):
            source_control = PackageEntry().load_control_from_wapt(packagename)
            if not newname:
                newname = source_control.package
            if target_directory == '':
                target_directory = self.get_default_development_dir(newname,section=source_control.section)
            if target_directory is None:
                target_directory = tempfile.mkdtemp('wapt')
            # check if we will not overwrite newer package or different package
            check_target_directory(target_directory,source_control)
            if packagename != target_directory:
                shutil.copytree(packagename,target_directory)
        # duplicate a wapt file
        elif os.path.isfile(packagename):
            source_filename = packagename
            source_control = PackageEntry().load_control_from_wapt(source_filename)
            if not newname:
                newname = source_control.package
            if target_directory == '':
                target_directory = self.get_default_development_dir(newname,section=source_control.section)
            if target_directory is None:
                target_directory = tempfile.mkdtemp('wapt')
            # check if we will not overwrite newer package or different package
            check_target_directory(target_directory,source_control)
            source_control.unzip_package(target_dir=target_directory,cabundle=cabundle)

        else:
            source_package = self.is_available(packagename)
            if not source_package:
                raise Exception('Package %s is not available is current repositories.'%(packagename,))
            # duplicate package from a repository
            filenames = self.download_packages([packagename],usecache=usecache,printhook=printhook)
            package_paths = filenames['downloaded'] or filenames['skipped']
            if not package_paths:
                raise Exception('Unable to download package %s'%(packagename,))
            source_filename = package_paths[0]
            source_control = PackageEntry().load_control_from_wapt(source_filename)
            if not newname:
                newname = source_control.package
            if target_directory == '':
                target_directory = self.get_default_development_dir(newname,section=source_control.section)
            if target_directory is None:
                target_directory = tempfile.mkdtemp('wapt')
            # check if we will not overwrite newer package or different package
            check_target_directory(target_directory,source_control)
            source_control.unzip_package(target_dir=target_directory,cabundle=cabundle)

        # duplicate package informations
        dest_control = PackageEntry()
        for a in source_control.required_attributes + source_control.optional_attributes:
            dest_control[a] = source_control[a]

        # add / remove dependencies from copy
        prev_depends = ensure_list(dest_control.depends)
        for d in append_depends:
            if not d in prev_depends:
                prev_depends.append(d)
        for d in remove_depends:
            if d in prev_depends:
                prev_depends.remove(d)
        dest_control.depends = ','.join(prev_depends)

        # add / remove conflicts from copy
        prev_conflicts = ensure_list(dest_control.conflicts)
        for d in append_conflicts:
            if not d in prev_conflicts:
                prev_conflicts.append(d)

        for d in remove_conflicts:
            if d in prev_conflicts:
                prev_conflicts.remove(d)
        dest_control.conflicts = ','.join(prev_conflicts)

        # change package name
        dest_control.package = newname
        if newversion:
            dest_control.version = newversion

        # Check existing versions of newname and increment it
        if auto_inc_version:
            older_packages = self.is_available(newname)
            if older_packages and dest_control<=older_packages[-1]:
                dest_control.version = older_packages[-1].version
                dest_control.inc_build()

        dest_control.filename = dest_control.make_package_filename()
        dest_control.save_control_to_wapt(target_directory)

        if dest_control.section != 'host':
            self.add_pyscripter_project(target_directory)
        dest_control.invalidate_signature()
        return dest_control

    def setup_tasks(self):
        """Setup cron job on windows for update and download-upgrade"""
        result = []
        # update and download new packages
        if setuphelpers.task_exists('wapt-update'):
            setuphelpers.delete_task('wapt-update')
        if self.config.has_option('global','waptupdate_task_period'):
            task = setuphelpers.create_daily_task(
                'wapt-update',
                sys.argv[0],
                '--update-packages download-upgrade',
                max_runtime=int(self.config.get('global','waptupdate_task_maxruntime')),
                repeat_minutes=int(self.config.get('global','waptupdate_task_period')))
            result.append('%s : %s' % ('wapt-update',task.GetTriggerString(0)))

        # upgrade of packages
        if setuphelpers.task_exists('wapt-upgrade'):
            setuphelpers.delete_task('wapt-upgrade')
        if self.config.has_option('global','waptupgrade_task_period'):
            task = setuphelpers.create_daily_task(
                'wapt-upgrade',
                sys.argv[0],
                '--update-packages upgrade',
                max_runtime=int(self.config.get('global','waptupgrade_task_maxruntime')),
                repeat_minutes= int(self.config.get('global','waptupgrade_task_period')))
            result.append('%s : %s' % ('wapt-upgrade',task.GetTriggerString(0)))
        return '\n'.join(result)

    def enable_tasks(self):
        """Enable Wapt automatic update/upgrade scheduling through windows scheduler"""
        result = []
        if setuphelpers.task_exists('wapt-upgrade'):
            setuphelpers.enable_task('wapt-upgrade')
            result.append('wapt-upgrade')
        if setuphelpers.task_exists('wapt-update'):
            setuphelpers.enable_task('wapt-update')
            result.append('wapt-update')
        return result

    def disable_tasks(self):
        """Disable Wapt automatic update/upgrade scheduling through windows scheduler"""
        result = []
        if setuphelpers.task_exists('wapt-upgrade'):
            setuphelpers.disable_task('wapt-upgrade')
            result.append('wapt-upgrade')
        if setuphelpers.task_exists('wapt-update'):
            setuphelpers.disable_task('wapt-update')
            result.append('wapt-update')
        return result

    def write_param(self,name,value):
        """Store in local db a key/value pair for later use"""
        self.waptdb.set_param(name,value)

    def read_param(self,name,default=None):
        """read a param value from local db
        >>> wapt = Wapt(config_filename='c:/wapt/wapt-get.ini')
        >>> wapt.read_param('db_version')
        u'20140410'
        """
        return self.waptdb.get_param(name,default)

    def delete_param(self,name):
        """Remove a key from local db"""
        self.waptdb.delete_param(name)

    def dependencies(self,packagename,expand=False):
        """Return all dependecies of a given package
        >>> w = Wapt(config_filename='c:/wapt/wapt-get.ini')
        >>> dep = w.dependencies('tis-waptdev')
        >>> isinstance(dep,list) and isinstance(dep[0],PackageEntry)
        True
        """
        packages = self.is_available(packagename)
        result = []
        errors = []
        if packages:
            depends = ensure_list(packages[-1].depends)
            for dep in depends:
                subpackages = self.is_available(dep)
                if subpackages:
                    if expand:
                        result.extend(self.dependencies(dep))
                    if not subpackages[-1] in result:
                        result.append(subpackages[-1])
                else:
                    errors.append(dep)

        return result

    def get_package_entries(self,packages_names):
        r"""Return most up to date packages entries for packages_names
        packages_names is either a list or a string
        return a dictionnary with {'packages':[],'missing':[]}
        >>> wapt = Wapt(config_filename='c:/wapt/wapt-get.ini')
        >>> res = wapt.get_package_entries(['tis-firefox','tis-putty'])
        >>> isinstance(res['missing'],list) and isinstance(res['packages'][0],PackageEntry)
        True
        """
        result = {'packages':[],'missing':[]}
        if isinstance(packages_names,str) or isinstance(packages_names,unicode):
            packages_names=[ p.strip() for p in packages_names.split(",")]
        for package_name in packages_names:
            matches = self.waptdb.packages_matching(package_name)
            if matches:
                result['packages'].append(matches[-1])
            else:
                result['missing'].append(package_name)
        return result


    def network_reconfigure(self):
        """Called whenever the network configuration has changed"""
        try:
            for repo in self.repositories:
                repo.reset_network()
            if not self.disable_update_server_status and self.waptserver_available():
                self.update_server_status()
        except Exception as e:
            logger.warning(u'WAPT was unable to reconfigure properly after network changes : %s'%ensure_unicode(e))

    def add_upgrade_shutdown_policy(self):
        """Add a local shitdown policy to upgrade system"""
        waptexit_path = setuphelpers.makepath(self.wapt_base_dir,'waptexit.exe')
        if not os.path.isfile(waptexit_path):
            raise Exception('Can not find %s'%waptexit_path)
        setuphelpers.shutdown_scripts_ui_visible(state=True)
        return setuphelpers.add_shutdown_script(waptexit_path,'')

    def remove_upgrade_shutdown_policy(self):
        """Add a local shitdown policy to upgrade system"""
        waptexit_path = setuphelpers.makepath(self.wapt_base_dir,'waptexit.exe')
        if not os.path.isfile(waptexit_path):
            raise Exception('Can not find %s'%waptexit_path)
        return setuphelpers.remove_shutdown_script(waptexit_path,'')


    def show_progress(self,show_box=False,msg='Loading...',progress = None,progress_max = None):
        if self.progress_hook:
            self.progress_hook(show_box,msg,progress,progress_max)  # pylint: disable=not-callable
        else:
            logger.debug('%s : %s / %s' % (msg,progress,progress_max))

def wapt_sources_edit(wapt_sources_dir):
    """Utility to open Pyscripter with package source if it is installed
        else open the development directory in Shell Explorer.

    Args
        wapt_sources_dir (str): directory path of  teh wapt package sources

    Returns:
        str: sources path
    """
    psproj_filename = os.path.join(wapt_sources_dir,'WAPT','wapt.psproj')
    control_filename = os.path.join(wapt_sources_dir,'WAPT','control')
    setup_filename = os.path.join(wapt_sources_dir,'setup.py')
    pyscripter_filename = os.path.join(setuphelpers.programfiles32,
                                       'PyScripter', 'PyScripter.exe')
    wapt_base_dir = os.path.dirname(__file__)
    env = os.environ
    env.update(dict(
        PYTHONHOME=wapt_base_dir,
        PYTHONPATH=wapt_base_dir,
        VIRTUAL_ENV=wapt_base_dir
        ))

    if os.path.isfile(pyscripter_filename) and os.path.isfile(psproj_filename):
        if not os.path.isfile(os.path.join(wapt_base_dir,'python.exe')):
            try:
                setuphelpers.run(r'mklink "%s\python.exe" "%s\Scripts\python.exe"' % (wapt_base_dir,wapt_base_dir))
            except Exception as e:
                raise Exception('Unable to start PySctipter properly. You should have python.exe in wapt base directory %s : %s' % (wapt_base_dir,e))
        p = psutil.Popen((u'"%s" --pythondllpath "%s" --python27 -N --project "%s" "%s" "%s"' % (
                         pyscripter_filename,
                         wapt_base_dir,
                         psproj_filename,
                         setup_filename,
                         control_filename)).encode(sys.getfilesystemencoding()),
                         cwd=wapt_sources_dir.encode(sys.getfilesystemencoding()),
                         env=env)
    else:
        os.startfile(wapt_sources_dir)
    return wapt_sources_dir


def sid_from_rid(domain_controller, rid):
    """Return SID structure based on supplied domain controller's domain and supplied rid
    rid can be for example DOMAIN_GROUP_RID_ADMINS, DOMAIN_GROUP_RID_USERS
    """
    umi2 = win32net.NetUserModalsGet(domain_controller, 2)
    domain_sid = umi2['domain_id']

    sub_authority_count = domain_sid.GetSubAuthorityCount()

    # create and init new sid with acct domain Sid + acct rid
    sid = pywintypes.SID()
    sid.Initialize(domain_sid.GetSidIdentifierAuthority(),
                   sub_authority_count+1)

    # copy existing subauthorities from account domain Sid into
    # new Sid
    for i in range(sub_authority_count):
        sid.SetSubAuthority(i, domain_sid.GetSubAuthority(i))

    # append Rid to new Sid
    sid.SetSubAuthority(sub_authority_count, rid)
    return sid


def lookup_name_from_rid(domain_controller, rid):
    """ return username or group name from RID (with localization if applicable)
        from https://mail.python.org/pipermail/python-win32/2006-May/004655.html
        domain_controller : should be a DC
        rid : integer number (512 for domain admins, 513 for domain users, etc.)
    >>> lookup_name_from_rid('srvads', DOMAIN_GROUP_RID_ADMINS)
    u'Domain Admins'

    """
    sid = sid_from_rid(domain_controller,rid)
    name, domain, typ = win32security.LookupAccountSid(domain_controller, sid)
    return name


def get_domain_admins_group_name():
    r""" return localized version of domain admin group (ie "domain admins" or
                 "administrateurs du domaine" with RID -512)
    >>> get_domain_admins_group_name()
    u'Domain Admins'
    """
    try:
        target_computer = win32net.NetGetAnyDCName ()
        name = lookup_name_from_rid(target_computer, DOMAIN_GROUP_RID_ADMINS)
        return name
    except Exception as e:
        logger.debug('Error getting Domain Admins group name : %s'%e)
        return 'Domain Admins'


def get_local_admins_group_name():
    sid = win32security.GetBinarySid('S-1-5-32-544')
    name, domain, typ = win32security.LookupAccountSid(setuphelpers.wincomputername(), sid)
    return name


def check_is_member_of(huser,group_name):
    """ check if a user is a member of a group
    huser : handle pywin32
    group_name : group as a string
    >>> from win32security import LogonUser
    >>> hUser = win32security.LogonUser ('technique','tranquilit','xxxxxxx',win32security.LOGON32_LOGON_NETWORK,win32security.LOGON32_PROVIDER_DEFAULT)
    >>> check_is_member_of(hUser,'domain admins')
    False
    """
    try:
        sid, system, type = win32security.LookupAccountName(None,group_name)
    except:
        logger.debug('"%s" is not a valid group name'%group_name)
        return False
    return win32security.CheckTokenMembership(huser, sid)


def check_user_membership(user_name,password,domain_name,group_name):
    """ check if a user is a member of a group
    user_name: user as a string
    password: as a string
    domain_name : as a string. If empty, check local then domain
    group_name : group as a string
    >>> from win32security import LogonUser
    >>> hUser = win32security.LogonUser ('technique','tranquilit','xxxxxxx',win32security.LOGON32_LOGON_NETWORK,win32security.LOGON32_PROVIDER_DEFAULT)
    >>> check_is_member_of(hUser,'domain admins')
    False
    """
    try:
        sid, system, type = win32security.LookupAccountName(None,group_name)
    except pywintypes.error as e:
        if e.args[0] == 1332:
            logger.warning('"%s" is not a valid group name'%group_name)
            return False
        else:
            raise
    huser = win32security.LogonUser(user_name,domain_name,password,win32security.LOGON32_LOGON_NETWORK,win32security.LOGON32_PROVIDER_DEFAULT)
    return win32security.CheckTokenMembership(huser, sid)

# for backward compatibility
Version = setuphelpers.Version  # obsolete

if __name__ == '__main__':
    import doctest
    import sys
    reload(sys)
    sys.setdefaultencoding("UTF-8")
    import doctest
    doctest.ELLIPSIS_MARKER = '???'
    doctest.testmod(optionflags=doctest.ELLIPSIS)
    sys.exit(0)
