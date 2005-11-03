# Copyright 2002 Ben Escoto
#
# This file is part of rdiff-backup.
#
# rdiff-backup is free software; you can redistribute it and/or modify
# under the terms of the GNU General Public License as published by the
# Free Software Foundation; either version 2 of the License, or (at your
# option) any later version.
#
# rdiff-backup is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with rdiff-backup; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307
# USA

"""Store and retrieve metadata in destination directory

The plan is to store metadata information for all files in the
destination directory in a special metadata file.  There are two
reasons for this:

1)  The filesystem of the mirror directory may not be able to handle
    types of metadata that the source filesystem can.  For instance,
    rdiff-backup may not have root access on the destination side, so
    cannot set uid/gid.  Or the source side may have ACLs and the
    destination side doesn't.

	Hopefully every file system can store binary data.  Storing
	metadata separately allows us to back up anything (ok, maybe
	strange filenames are still a problem).

2)  Metadata can be more quickly read from a file than it can by
    traversing the mirror directory over and over again.  In many
    cases most of rdiff-backup's time is spent compaing metadata (like
    file size and modtime), trying to find differences.  Reading this
    data sequentially from a file is significantly less taxing than
    listing directories and statting files all over the mirror
    directory.

The metadata is stored in a text file, which is a bunch of records
concatenated together.  Each record has the format:

File <filename>
  <field_name1> <value>
  <field_name2> <value>
  ...

Where the lines are separated by newlines.  See the code below for the
field names and values.

"""

from __future__ import generators
import re, gzip, os, binascii
import log, Globals, rpath, Time, robust, increment, static, rorpiter

class ParsingError(Exception):
	"""This is raised when bad or unparsable data is received"""
	pass

def carbonfile2string(cfile):
	"""Convert CarbonFile data to a string suitable for storing."""
	retvalparts = []
	retvalparts.append('creator:%s' % binascii.hexlify(cfile['creator']))
	retvalparts.append('type:%s' % binascii.hexlify(cfile['type']))
	retvalparts.append('location:%d,%d' % cfile['location'])
	retvalparts.append('flags:%d' % cfile['flags'])
	return '|'.join(retvalparts)

def string2carbonfile(data):
	"""Re-constitute CarbonFile data from a string stored by 
	carbonfile2string."""
	retval = {}
	for component in data.split('|'):
		key, value = component.split(':')
		if key == 'creator':
			retval['creator'] = binascii.unhexlify(value)
		elif key == 'type':
			retval['type'] = binascii.unhexlify(value)
		elif key == 'location':
			a, b = value.split(',')
			retval['location'] = (int(a), int(b))
		elif key == 'flags':
			retval['flags'] = int(value)
	return retval

def RORP2Record(rorpath):
	"""From RORPath, return text record of file's metadata"""
	str_list = ["File %s\n" % quote_path(rorpath.get_indexpath())]

	# Store file type, e.g. "dev", "reg", or "sym", and type-specific data
	type = rorpath.gettype()
	if type is None: type = "None"
	str_list.append("  Type %s\n" % type)
	if type == "reg":
		str_list.append("  Size %s\n" % rorpath.getsize())

		# If there is a resource fork, save it.
		if rorpath.has_resource_fork():
			if not rorpath.get_resource_fork(): rf = "None"
			else: rf = binascii.hexlify(rorpath.get_resource_fork())
			str_list.append("  ResourceFork %s\n" % (rf,))
                
		# If there is Carbon data, save it.
		if rorpath.has_carbonfile():
			if not rorpath.get_carbonfile(): cfile = "None"
			else: cfile = carbonfile2string(rorpath.get_carbonfile())
			str_list.append("  CarbonFile %s\n" % (cfile,))

		# If file is hardlinked, add that information
		if Globals.preserve_hardlinks:
			numlinks = rorpath.getnumlinks()
			if numlinks > 1:
				str_list.append("  NumHardLinks %s\n" % numlinks)
				str_list.append("  Inode %s\n" % rorpath.getinode())
				str_list.append("  DeviceLoc %s\n" % rorpath.getdevloc())

		# Save any hashes, if available
		if rorpath.has_sha1():
			str_list.append('  SHA1Digest %s\n' % rorpath.get_sha1())

	elif type == "None": return "".join(str_list)
	elif type == "dir" or type == "sock" or type == "fifo": pass
	elif type == "sym":
		str_list.append("  SymData %s\n" % quote_path(rorpath.readlink()))
	elif type == "dev":
		major, minor = rorpath.getdevnums()
		if rorpath.isblkdev(): devchar = "b"
		else:
			assert rorpath.ischardev()
			devchar = "c"
		str_list.append("  DeviceNum %s %s %s\n" % (devchar, major, minor))

	# Store time information
	if type != 'sym' and type != 'dev':
		str_list.append("  ModTime %s\n" % rorpath.getmtime())

	# Add user, group, and permission information
	uid, gid = rorpath.getuidgid()
	str_list.append("  Uid %s\n" % uid)
	str_list.append("  Uname %s\n" % (rorpath.getuname() or ":"))
	str_list.append("  Gid %s\n" % gid)
	str_list.append("  Gname %s\n" % (rorpath.getgname() or ":"))
	str_list.append("  Permissions %s\n" % rorpath.getperms())
	return "".join(str_list)

line_parsing_regexp = re.compile("^ *([A-Za-z0-9]+) (.+)$", re.M)
def Record2RORP(record_string):
	"""Given record_string, return RORPath

	For speed reasons, write the RORPath data dictionary directly
	instead of calling rorpath functions.  Profiling has shown this to
	be a time critical function.

	"""
	data_dict = {}
	for field, data in line_parsing_regexp.findall(record_string):
		if field == "File": index = quoted_filename_to_index(data)
		elif field == "Type":
			if data == "None": data_dict['type'] = None
			else: data_dict['type'] = data
		elif field == "Size": data_dict['size'] = long(data)
		elif field == "ResourceFork":
			if data == "None": data_dict['resourcefork'] = ""
			else: data_dict['resourcefork'] = binascii.unhexlify(data)
		elif field == "CarbonFile":
			if data == "None": data_dict['carbonfile'] = None
			else: data_dict['carbonfile'] = string2carbonfile(data)
		elif field == "SHA1Digest": data_dict['sha1'] = data
		elif field == "NumHardLinks": data_dict['nlink'] = int(data)
		elif field == "Inode": data_dict['inode'] = long(data)
		elif field == "DeviceLoc": data_dict['devloc'] = long(data)
		elif field == "SymData": data_dict['linkname'] = unquote_path(data)
		elif field == "DeviceNum":
			devchar, major_str, minor_str = data.split(" ")
			data_dict['devnums'] = (devchar, int(major_str), int(minor_str))
		elif field == "ModTime": data_dict['mtime'] = long(data)
		elif field == "Uid": data_dict['uid'] = int(data)
		elif field == "Gid": data_dict['gid'] = int(data)
		elif field == "Uname":
			if data == ":" or data == 'None': data_dict['uname'] = None
			else: data_dict['uname'] = data
		elif field == "Gname":
			if data == ':' or data == 'None': data_dict['gname'] = None
			else: data_dict['gname'] = data
		elif field == "Permissions": data_dict['perms'] = int(data)
		else: raise ParsingError("Unknown field in line '%s %s'" %
								 (field, data))
	return rpath.RORPath(index, data_dict)

chars_to_quote = re.compile("\\n|\\\\")
def quote_path(path_string):
	"""Return quoted verson of path_string

	Because newlines are used to separate fields in a record, they are
	replaced with \n.  Backslashes become \\ and everything else is
	left the way it is.

	"""
	def replacement_func(match_obj):
		"""This is called on the match obj of any char that needs quoting"""
		char = match_obj.group(0)
		if char == "\n": return "\\n"
		elif char == "\\": return "\\\\"
		assert 0, "Bad char %s needs quoting" % char
	return chars_to_quote.sub(replacement_func, path_string)

def unquote_path(quoted_string):
	"""Reverse what was done by quote_path"""
	def replacement_func(match_obj):
		"""Unquote match obj of two character sequence"""
		two_chars = match_obj.group(0)
		if two_chars == "\\n": return "\n"
		elif two_chars == "\\\\": return "\\"
		log.Log("Warning, unknown quoted sequence %s found" % two_chars, 2)
		return two_chars
	return re.sub("\\\\n|\\\\\\\\", replacement_func, quoted_string)

def quoted_filename_to_index(quoted_filename):
	"""Return tuple index given quoted filename"""
	if quoted_filename == '.': return ()
	else: return tuple(unquote_path(quoted_filename).split('/'))

class FlatExtractor:
	"""Controls iterating objects from flat file"""

	# Set this in subclass.  record_boundary_regexp should match
	# beginning of next record.  The first group should start at the
	# beginning of the record.  The second group should contain the
	# (possibly quoted) filename.
	record_boundary_regexp = None

	# Set in subclass to function that converts text record to object
	record_to_object = None

	def __init__(self, fileobj):
		self.fileobj = fileobj # holds file object we are reading from
		self.buf = "" # holds the next part of the file
		self.at_end = 0 # True if we are at the end of the file
		self.blocksize = 32 * 1024

	def get_next_pos(self):
		"""Return position of next record in buffer, or end pos if none"""
		while 1:
			m = self.record_boundary_regexp.search(self.buf, 1)
			if m: return m.start(1)
			else: # add next block to the buffer, loop again
				newbuf = self.fileobj.read(self.blocksize)
				if not newbuf:
					self.at_end = 1
					return len(self.buf)
				else: self.buf += newbuf

	def iterate(self):
		"""Return iterator that yields all objects with records"""
		for record in self.iterate_records():
			try: yield self.record_to_object(record)
			except ParsingError, e:
				if self.at_end: break # Ignore whitespace/bad records at end
				log.Log("Error parsing flat file: %s" % (e,), 2)

	def iterate_records(self):
		"""Yield all text records in order"""
		while 1:
			next_pos = self.get_next_pos()
			yield self.buf[:next_pos]
			if self.at_end: break
			self.buf = self.buf[next_pos:]
		assert not self.fileobj.close()

	def skip_to_index(self, index):
		"""Scan through the file, set buffer to beginning of index record

		Here we make sure that the buffer always ends in a newline, so
		we will not be splitting lines in half.

		"""
		assert not self.buf or self.buf.endswith("\n")
		while 1:
			self.buf = self.fileobj.read(self.blocksize)
			self.buf += self.fileobj.readline()
			if not self.buf:
				self.at_end = 1
				return
			while 1:
				m = self.record_boundary_regexp.search(self.buf)
				if not m: break
				cur_index = self.filename_to_index(m.group(2))
				if cur_index >= index:
					self.buf = self.buf[m.start(1):]
					return
				else: self.buf = self.buf[m.end(1):]

	def iterate_starting_with(self, index):
		"""Iterate objects whose index starts with given index"""
		self.skip_to_index(index)
		if self.at_end: return
		while 1:
			next_pos = self.get_next_pos()
			try: obj = self.record_to_object(self.buf[:next_pos])
			except ParsingError, e:
				log.Log("Error parsing metadata file: %s" % (e,), 2)
			else:
				if obj.index[:len(index)] != index: break
				yield obj
			if self.at_end: break
			self.buf = self.buf[next_pos:]
		assert not self.fileobj.close()

	def filename_to_index(self, filename):
		"""Translate filename, possibly quoted, into an index tuple

		The filename is the first group matched by
		regexp_boundary_regexp.

		"""
		assert 0 # subclass


class RorpExtractor(FlatExtractor):
	"""Iterate rorps from metadata file"""
	record_boundary_regexp = re.compile("(?:\\n|^)(File (.*?))\\n")
	record_to_object = staticmethod(Record2RORP)
	filename_to_index = staticmethod(quoted_filename_to_index)


class FlatFile:
	"""Manage a flat (probably text) file containing info on various files

	This is used for metadata information, and possibly EAs and ACLs.
	The main read interface is as an iterator.  The storage format is
	a flat, probably compressed file, so random access is not
	recommended.

	"""
	rp, fileobj, mode = None, None, None
	_buffering_on = 1 # Buffering may be useful because gzip writes are slow
	_record_buffer, _max_buffer_size = None, 100
	_extractor = FlatExtractor # Override to class that iterates objects
	_object_to_record = None # Set to function converting object to record
	_prefix = None # Set to required prefix
	def __init__(self, rp, mode):
		"""Open rp for reading ('r') or writing ('w')"""
		self.rp = rp
		self.mode = mode
		self._record_buffer = []
		assert rp.isincfile() and rp.getincbase_str() == self._prefix, rp
		if mode == 'r':
			self.fileobj = self.rp.open("rb", rp.isinccompressed())
		else:
			assert mode == 'w' and not self.rp.lstat(), (mode, rp)
			self.fileobj = self.rp.open("wb", rp.isinccompressed())

	def write_record(self, record):
		"""Write a (text) record into the file"""
		if self._buffering_on:
			self._record_buffer.append(record)
			if len(self._record_buffer) >= self._max_buffer_size:
				self.fileobj.write("".join(self._record_buffer))
				self._record_buffer = []
		else: self.fileobj.write(record)

	def write_object(self, object):
		"""Convert one object to record and write to file"""
		self.write_record(self._object_to_record(object))

	def get_objects(self, restrict_index = None):
		"""Return iterator of objects records from file rp"""
		if not restrict_index: return self._extractor(self.fileobj).iterate()
		extractor = self._extractor(self.fileobj)
		return extractor.iterate_starting_with(restrict_index)

	def get_records(self):
		"""Return iterator of text records"""
		return self._extractor(self.fileobj).iterate_records()

	def close(self):
		"""Close file, for when any writing is done"""
		assert self.fileobj, "File already closed"
		if self._buffering_on and self._record_buffer: 
			self.fileobj.write("".join(self._record_buffer))
			self._record_buffer = []
		try: fileno = self.fileobj.fileno() # will not work if GzipFile
		except AttributeError: fileno = self.fileobj.fileobj.fileno()
		os.fsync(fileno)
		result = self.fileobj.close()
		self.fileobj = None
		self.rp.setdata()
		return result

class MetadataFile(FlatFile):
	"""Store/retrieve metadata from mirror_metadata as rorps"""
	_prefix = "mirror_metadata"
	_extractor = RorpExtractor
	_object_to_record = staticmethod(RORP2Record)


class CombinedWriter:
	"""Used for simultaneously writting metadata, eas, and acls"""
	def __init__(self, metawriter, eawriter, aclwriter):
		self.metawriter = metawriter
		self.eawriter, self.aclwriter = eawriter, aclwriter # these can be None

	def write_object(self, rorp):
		"""Write information in rorp to all the writers"""
		self.metawriter.write_object(rorp)
		if self.eawriter and not rorp.get_ea().empty():
			self.eawriter.write_object(rorp.get_ea())
		if self.aclwriter and not rorp.get_acl().is_basic():
			self.aclwriter.write_object(rorp.get_acl())

	def close(self):
		self.metawriter.close()
		if self.eawriter: self.eawriter.close()
		if self.aclwriter: self.aclwriter.close()


class Manager:
	"""Read/Combine/Write metadata files by time"""
	meta_prefix = 'mirror_metadata'
	acl_prefix = 'access_control_lists'
	ea_prefix = 'extended_attributes'

	def __init__(self):
		"""Set listing of rdiff-backup-data dir"""
		self.rplist = []
		self.timerpmap = {}
		for filename in Globals.rbdir.listdir():
			rp = Globals.rbdir.append(filename)
			if rp.isincfile():
				self.rplist.append(rp)
				time = rp.getinctime()
				if self.timerpmap.has_key(time):
					self.timerpmap[time].append(rp)
				else: self.timerpmap[time] = [rp]
				
	def _iter_helper(self, prefix, flatfileclass, time, restrict_index):
		"""Used below to find the right kind of file by time"""
		if not self.timerpmap.has_key(time): return None
		for rp in self.timerpmap[time]:
			if rp.getincbase_str() == prefix:
				return flatfileclass(rp, 'r').get_objects(restrict_index)
		return None

	def get_meta_at_time(self, time, restrict_index):
		"""Return iter of metadata rorps at given time (or None)"""
		return self._iter_helper(self.meta_prefix, MetadataFile,
								 time, restrict_index)

	def get_eas_at_time(self, time, restrict_index):
		"""Return Extended Attributes iter at given time (or None)"""
		return self._iter_helper(self.ea_prefix,
					  eas_acls.ExtendedAttributesFile, time, restrict_index)

	def get_acls_at_time(self, time, restrict_index):
		"""Return ACLs iter at given time from recordfile (or None)"""
		return self._iter_helper(self.acl_prefix,
					  eas_acls.AccessControlListFile, time, restrict_index)

	def GetAtTime(self, time, restrict_index = None):
		"""Return combined metadata iter with ea/acl info if necessary"""
		cur_iter = self.get_meta_at_time(time, restrict_index)
		if not cur_iter:
			log.Log("Warning, could not find mirror_metadata file.\n"
					"Metadata will be read from filesystem instead.", 2)
			return None

		if Globals.acls_active:
			acl_iter = self.get_acls_at_time(time, restrict_index)
			if not acl_iter:
				log.Log("Warning: Access Control List file not found", 2)
				acl_iter = iter([])
			cur_iter = eas_acls.join_acl_iter(cur_iter, acl_iter)
		if Globals.eas_active:
			ea_iter = self.get_eas_at_time(time, restrict_index)
			if not ea_iter:
				log.Log("Warning: Extended Attributes file not found", 2)
				ea_iter = iter([])
			cur_iter = eas_acls.join_ea_iter(cur_iter, ea_iter)
		return cur_iter

	def _writer_helper(self, prefix, flatfileclass, typestr, time):
		"""Used in the get_xx_writer functions, returns a writer class"""
		if time is None: timestr = Time.curtimestr
		else: timestr = Time.timetostring(time)		
		filename = '%s.%s.%s.gz' % (prefix, timestr, typestr)
		rp = Globals.rbdir.append(filename)
		assert not rp.lstat(), "File %s already exists!" % (rp.path,)
		return flatfileclass(rp, 'w')

	def get_meta_writer(self, typestr, time):
		"""Return MetadataFile object opened for writing at given time"""
		return self._writer_helper(self.meta_prefix, MetadataFile,
								   typestr, time)

	def get_ea_writer(self, typestr, time):
		"""Return ExtendedAttributesFile opened for writing"""
		return self._writer_helper(self.ea_prefix,
						 eas_acls.ExtendedAttributesFile, typestr, time)

	def get_acl_writer(self, typestr, time):
		"""Return AccessControlListFile opened for writing"""
		return self._writer_helper(self.acl_prefix,
						 eas_acls.AccessControlListFile, typestr, time)

	def GetWriter(self, typestr = 'snapshot', time = None):
		"""Get a writer object that can write meta and possibly acls/eas"""
		metawriter = self.get_meta_writer(typestr, time)
		if not Globals.eas_active and not Globals.acls_active:
			return metawriter # no need for a CombinedWriter

		if Globals.eas_active: ea_writer = self.get_ea_writer(typestr, time)
		if Globals.acls_active: acl_writer = self.get_acl_writer(typestr, time)
		return CombinedWriter(metawriter, ea_writer, acl_writer)

ManagerObj = None # Set this later to Manager instance
def SetManager():
	global ManagerObj
	ManagerObj = Manager()


def patch(*meta_iters):
	"""Return an iterator of metadata files by combining all the given iters

	The iters should be given as a list/tuple in reverse chronological
	order.  The earliest rorp in each iter will supercede all the
	later ones.

	"""
	for meta_tuple in rorpiter.CollateIterators(*meta_iters):
		for i in range(len(meta_tuple)-1, -1, -1):
			if meta_tuple[i]:
				if meta_tuple[i].lstat(): yield meta_tuple[i]
				break # move to next index
		else: assert 0, "No valid rorps"

def Convert_diff(cur_time, old_time):
	"""Convert the metadata snapshot at old_time to diff format

	The point is just to save space.  The diff format is simple, just
	include in the diff all of the older rorps that are different in
	the two metadata rorps.

	"""
	rblist = [Globals.rbdir.append(filename)
			  for filename in robust.listrp(Globals.rbdir)]
	cur_iter = MetadataFile.get_objects_at_time(
		Globals.rbdir, cur_time, None, rblist)
	old_iter = MetadataFile.get_objects_at_time(
		Globals.rbdir, old_time, None, rblist)
	assert cur_iter.type == old_iter.type == 'snapshot'
	diff_file = MetadataFile.open_file(None, 1, 'diff', old_time)

	for cur_rorp, old_rorp in rorpiter.Collate2Iters(cur_iter, old_iter):
		XXX


import eas_acls # put at bottom to avoid python circularity bug
