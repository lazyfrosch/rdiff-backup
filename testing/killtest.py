import unittest, os, signal, sys, random, time
execfile("commontest.py")
rbexec("main.py")

"""Test consistency by killing rdiff-backup as it is backing up"""

Log.setverbosity(3)

class Local:
	"""Hold some local RPaths"""
	def get_local_rp(ext):
		return RPath(Globals.local_connection, "testfiles/" + ext)

	inc1rp = get_local_rp('increment1')
	inc2rp = get_local_rp('increment2')
	inc3rp = get_local_rp('increment3')
	inc4rp = get_local_rp('increment4')

	rpout = get_local_rp('output')
	rpout_inc = get_local_rp('output_inc')
	rpout1 = get_local_rp('restoretarget1')
	rpout2 = get_local_rp('restoretarget2')
	rpout3 = get_local_rp('restoretarget3')
	rpout4 = get_local_rp('restoretarget4')
	rpout5 = get_local_rp('restoretarget5')

	back1 = get_local_rp('backup1')
	back2 = get_local_rp('backup2')
	back3 = get_local_rp('backup3')
	back4 = get_local_rp('backup4')
	back5 = get_local_rp('backup5')

class TimingError(Exception):
	"""Indicates timing error - process killed too soon or too late"""
	pass


class ProcessFuncs(unittest.TestCase):
	"""Subclassed by Resume and NoResume"""
	def delete_tmpdirs(self):
		"""Remove any temp directories created by previous tests"""
		assert not os.system(MiscDir + '/myrm testfiles/output* '
							 'testfiles/restoretarget* testfiles/vft_out '
							 'timbar.pyc testfiles/vft2_out')

	def is_aborted_backup(self):
		"""True if there are signs of aborted backup in output/"""
		try: dirlist = os.listdir("testfiles/output/rdiff-backup-data")
		except OSError:
			raise TimingError("No data dir found, give process more time")
		dirlist = filter(lambda f: f.startswith("last-file-incremented"),
						 dirlist)
		return len(dirlist) != 0

	def exec_rb(self, time, wait, *args):
		"""Run rdiff-backup return pid"""
		arglist = ['python', '../src/rdiff-backup', '-v7']
		if time:
			arglist.append("--current-time")
			arglist.append(str(time))
		arglist.extend(args)

		print "Running ", arglist
		if wait: return os.spawnvp(os.P_WAIT, 'python', arglist)
		else: return os.spawnvp(os.P_NOWAIT, 'python', arglist)

	def exec_and_kill(self, mintime, maxtime, backup_time, resume, arg1, arg2):
		"""Run rdiff-backup, then kill and run again

		Kill after a time between mintime and maxtime.  First process
		should not terminate before maxtime.

		"""
		pid = self.exec_rb(backup_time, None, arg1, arg2)
		time.sleep(random.uniform(mintime, maxtime))
		if os.waitpid(pid, os.WNOHANG)[0] != 0:
			raise TimingError("Timing Error on %s, %s:\n"
							  "Process already quit - try lowering max time"
							  % (arg1, arg2))
		os.kill(pid, self.killsignal)
		while 1:
			pid, exitstatus = os.waitpid(pid, os.WNOHANG)
			if pid:
				assert exitstatus != 0
				break
			time.sleep(0.2)
		if not self.is_aborted_backup():
			raise TimingError("Timing Error on %s, %s:\n"
							  "Process already finished or didn't "
							  "get a chance to start" % (arg1, arg2))
		print "---------------------- killed"
		os.system("ls -l %s/rdiff-backup-data" % arg1)
		if resume: self.exec_rb(backup_time + 5, 1, '--resume', arg1, arg2)
		else: self.exec_rb(backup_time + 5000, 1, '--no-resume', arg1, arg2)

	def verify_back_dirs(self):
		"""Make sure testfiles/output/back? dirs exist"""
		if (Local.back1.lstat() and Local.back2.lstat() and
			Local.back3.lstat() and Local.back4.lstat() and
			Local.back5.lstat()): return

		os.system(MiscDir + "/myrm testfiles/backup[1-5]")

		self.exec_rb(10000, 1, 'testfiles/increment3', 'testfiles/backup1')
		Local.back1.setdata()

		self.exec_rb(10000, 1, 'testfiles/increment3', 'testfiles/backup2')
		self.exec_rb(20000, 1, 'testfiles/increment1', 'testfiles/backup2')
		Local.back2.setdata()
		
		self.exec_rb(10000, 1, 'testfiles/increment3', 'testfiles/backup3')
		self.exec_rb(20000, 1, 'testfiles/increment1', 'testfiles/backup3')
		self.exec_rb(30000, 1, 'testfiles/increment2', 'testfiles/backup3')
		Local.back3.setdata()
		
		self.exec_rb(10000, 1, 'testfiles/increment3', 'testfiles/backup4')
		self.exec_rb(20000, 1, 'testfiles/increment1', 'testfiles/backup4')
		self.exec_rb(30000, 1, 'testfiles/increment2', 'testfiles/backup4')
		self.exec_rb(40000, 1, 'testfiles/increment3', 'testfiles/backup4')
		Local.back4.setdata()

		self.exec_rb(10000, 1, 'testfiles/increment3', 'testfiles/backup5')
		self.exec_rb(20000, 1, 'testfiles/increment1', 'testfiles/backup5')
		self.exec_rb(30000, 1, 'testfiles/increment2', 'testfiles/backup5')
		self.exec_rb(40000, 1, 'testfiles/increment3', 'testfiles/backup5')
		self.exec_rb(50000, 1, 'testfiles/increment4', 'testfiles/backup5')
		Local.back5.setdata()

	def runtest_sequence(self, total_tests,
						 exclude_rbdir, ignore_tmp, compare_links,
						 stop_on_error = None):
		timing_problems, failures = 0, 0
		for i in range(total_tests):
			try:
				result = self.runtest(exclude_rbdir, ignore_tmp, compare_links)
			except TimingError, te:
				print te
				timing_problems += 1
				continue
			if result != 1:
				if stop_on_error: assert 0, "Compare Failure"
				else: failures += 1

		print total_tests, "tests attempted total"
		print "%s setup problems, %s failures, %s successes" % \
			  (timing_problems, failures,
			   total_tests - timing_problems - failures)		


class Resume(ProcessFuncs):
	"""Test for graceful recovery after resumed backup"""
	def runtest(self, exclude_rbdir, ignore_tmp_files, compare_links):
		"""Run the actual test, returning 1 if passed and 0 otherwise"""
		self.delete_tmpdirs()
		self.verify_back_dirs()
		
		# Backing up increment3

		# Start with increment3 because it is big and the first case
		# is kind of special (there's no incrementing, so different
		# code)
		self.exec_and_kill(0.7, 1.5, 10000, 1,
						   'testfiles/increment3', 'testfiles/output')
		if not CompareRecursive(Local.back1, Local.rpout, compare_links,
								None, exclude_rbdir, ignore_tmp_files):
			return 0

		# Backing up increment1
		self.exec_and_kill(0.8, 0.8, 20000, 1,
						   'testfiles/increment1', 'testfiles/output')
		if not CompareRecursive(Local.back2, Local.rpout, compare_links,
								None, exclude_rbdir, ignore_tmp_files):
			return 0

		# Backing up increment2
		self.exec_and_kill(0.7, 1.0, 30000, 1,
						   'testfiles/increment2', 'testfiles/output')
		if not CompareRecursive(Local.back3, Local.rpout, compare_links,
								None, exclude_rbdir, ignore_tmp_files):
			return 0

		# Backing up increment3
		self.exec_and_kill(0.7, 2.0, 40000, 1,
						   'testfiles/increment3', 'testfiles/output')
		if not CompareRecursive(Local.back4, Local.rpout, compare_links,
								None, exclude_rbdir, ignore_tmp_files):
			return 0

		# Backing up increment4
		self.exec_and_kill(1.0, 5.0, 50000, 1,
						   'testfiles/increment4', 'testfiles/output')
		if not CompareRecursive(Local.back5, Local.rpout, compare_links,
								None, exclude_rbdir, ignore_tmp_files):
			return 0
		return 1

	def testTERM(self, total_tests = 3):
		"""Test sending local processes a TERM signal"""
		self.killsignal = signal.SIGTERM
		self.runtest_sequence(total_tests, None, None, 1)

	def testKILL(self, total_tests = 10):
		"""Send local backup process a KILL signal"""
		self.killsignal = signal.SIGKILL
		self.runtest_sequence(total_tests, None, 1, None)


class NoResume(ProcessFuncs):
	"""Test for consistent backup after abort and then no resume"""
	def runtest(self, exclude_rbdir, ignore_tmp_files, compare_links):
		self.delete_tmpdirs()

		# Back up each increment to output
		self.exec_and_kill(0.7, 1.5, 10000, 1,
						   'testfiles/increment3', 'testfiles/output')
		self.exec_and_kill(0.6, 0.6, 20000, 1,
						   'testfiles/increment1', 'testfiles/output')
		self.exec_and_kill(0.7, 1.0, 30000, 1,
						   'testfiles/increment2', 'testfiles/output')
		self.exec_and_kill(0.7, 2.0, 40000, 1,
						   'testfiles/increment3', 'testfiles/output')
		self.exec_and_kill(1.0, 5.0, 50000, 1,
						   'testfiles/increment4', 'testfiles/output')

		# Now restore each and compare
		InternalRestore(1, 1, "testfiles/output", "testfiles/restoretarget1",
						15000)
		assert CompareRecursive(Local.inc3rp, Local.rpout1, compare_links,
								None, exclude_rbdir, ignore_tmp_files)
		InternalRestore(1, 1, "testfiles/output", "testfiles/restoretarget2",
						25000)
		assert CompareRecursive(Local.inc1rp, Local.rpout2, compare_links,
								None, exclude_rbdir, ignore_tmp_files)
		InternalRestore(1, 1, "testfiles/output", "testfiles/restoretarget3",
						35000)
		assert CompareRecursive(Local.inc2rp, Local.rpout3, compare_links,
								None, exclude_rbdir, ignore_tmp_files)
		InternalRestore(1, 1, "testfiles/output", "testfiles/restoretarget4",
						45000)
		assert CompareRecursive(Local.inc3rp, Local.rpout4, compare_links,
								None, exclude_rbdir, ignore_tmp_files)
		InternalRestore(1, 1, "testfiles/output", "testfiles/restoretarget5",
						55000)
		assert CompareRecursive(Local.inc4rp, Local.rpout5, compare_links,
								None, exclude_rbdir, ignore_tmp_files)
		return 1

	def testTERM(self, total_tests = 10):
		self.killsignal = signal.SIGTERM
		self.runtest_sequence(total_tests, 1, None, 1)

	def testKILL(self, total_tests = 20):
		self.killsignal = signal.SIGKILL
		self.runtest_sequence(total_tests, 1, 1, None)


if __name__ == "__main__": unittest.main()
