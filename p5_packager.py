#
# Simple stupid script to package up js projects
# in the way we need for our workflow
#
# TODO:
#
import copy
import datetime
import fnmatch
import json
import math
import optparse
import os
import shutil
import sys
import re
import tempfile
import time
import types
import uuid
import distutils.util
import string
import subprocess
pj = os.path.join


def main():
   options = parseOptions()

   # Do stuff
   proj = Project()
   proj.loadConfig(options.config_file)

   all_build_ids = [build.key for build in proj.builds]

   # If the user did not specify a build target, or specified the default all
   # target, then only use builds that are not hidden. Otherwise only build the
   # specified build.
   builds_ids = []
   if options.build == "all":
      build_ids = [build.key for build in proj.builds if not build.hidden]
   else:
      assert options.build in all_build_ids, "Invalid build specified"
      build_ids = [options.build]

   if options.clobber:
      proj.clobber(build_ids)
   elif options.monitor:
      proj.runMonitoredBuild(build_ids, int(options.interval))
   else:
      proj.runBuild(build_ids)
      print "Done"


def parseOptions():
   usage = "usage: %prog [options] config_file" + \
           "  config_file: must be a json configuration object or" + \
           "  python code creating a 'config' dictionary"
   parser = optparse.OptionParser(usage=usage)
   parser.add_option("-b", "--build", default = "all",
               help = "The build configuration to run. [%default]")
   parser.add_option("--monitor", action="store_true", default = False,
               help = "If true, then monitor the files and update dynamically")
   parser.add_option("--interval", type = "int", default = 1,
               help = "Number of seconds to wait between checks for build changes. [%default]")
   parser.add_option("--clobber", action="store_true", default = False,
               help = "Clean up the build area by removing the target directories.")

   (options, args) = parser.parse_args()

   if len(args) != 1:
      parser.print_help()
      sys.exit(1)

   fname = os.path.abspath(args[0])
   options.config_file = fname

   if not os.path.exists(fname):
      print "File does not exist: ", fname
      parser.print_help()
      sys.exit(1)

   return options


class Project(object):
   """
   The "kitchen sink" that does everything for a configured project.
   """
   def __init__(self):
      self.name       = ""
      self.packages   = []
      self.builds     = []
      self.rawConfig  = None
      self.confFile   = ""

   # --- Configuration Related ---- #
   def getPackage(self, key):
      for pkg in self.packages:
         if pkg.key == key:
            return pkg
      return None

   def getBuild(self, key):
      for build in self.builds:
         if build.key == key:
            return build
      return None

   def loadConfig(self, confFile):
      """ Load configuration file and process it into settings. """
      self.confFile = confFile

      try:
         namespace = {}
         execfile(self.confFile, namespace)
         if not namespace.has_key("config"):
            self.rawConfig = json.loads(open(self.confFile, 'r').read())
         else:
            self.rawConfig = namespace["config"]
      except ValueError, e:
         raise SystemExit(e)

      self.config()

   def config(self):
      # Clear the old settings (if any)
      self.packages = []
      self.builds   = []

      self.name = self.rawConfig.get("project", "")

      # build up packages
      package_configs = self.rawConfig.get("packages", [])
      for pkg_cfg in package_configs:
         key = pkg_cfg.get("id", None)
         if key is None:
            key = str(uuid.uuid4())
         pkg = Package(key)
         pkg.config(pkg_cfg)
         self.packages.append(pkg)

      # Build up builds
      build_configs = self.rawConfig.get("builds", {})
      for (k, build_cfg) in build_configs.items():
         build = Build(k)
         build.config(build_cfg)
         self.builds.append(build)

   #{ Processing Related
   def clobber(self, buildIds):
      """
      Run clobber for the given build key.
      """
      for buildKey in buildIds:
         print "Clobbering build: %s" % buildKey
         build_config = self.getBuild(buildKey)
         if build_config is None:
            raise RuntimeError("No config found for build")
         target_dir = build_config.targetDir
         if os.path.exists(target_dir):
            print "removing: %s" % target_dir
            shutil.rmtree(target_dir)

   def runBuild(self, buildIds):
      """
      Run a build and put it into place
      """
      for buildKey in buildIds:
         print "Running build: %s" % buildKey

         build_config = self.getBuild(buildKey)
         if build_config is None:
            raise RuntimeError("No config found for build")
         grouped_files = self.getMergedFileGroup(buildKey)
         target_dir    = build_config.targetDir

         # If we are compressing the javascript files, then compress them in
         # place if the files have changed and replace them in the subst map
         if build_config.compressJsLevel > 0:
            src_compressed_file = build_config.compressedJsFilename
            target_compressed_file = pj(target_dir, build_config.compressedJsFilename)

            js_files_have_changed = False
            js_files = grouped_files['js_files']
            # If any source file is newer than the last compressed version, we need to rebuild
            for fname in js_files:
               if self.fileHasChanged(fname, src_compressed_file,
                                      compareSize = False, compareTime = True):
                  print "%s has changed. Regen compressed file" % fname
                  js_files_have_changed = True
                  break

            # If they have changed, then we need to build a compressed file
            if js_files_have_changed:
               self.compressJsFiles(build_config.compressJsLevel, js_files, src_compressed_file)
            grouped_files["js_files"] = [src_compressed_file]


         # Copy files
         # - special handling for subst files because we will regen them every time
         for (fg_key, files) in grouped_files.iteritems():
            is_subst_files = (fg_key == 'subst_files')
            for fname in files:
               target_fname = pj(target_dir, fname)
               if self.fileHasChanged(fname, target_fname) or is_subst_files:
                  print "%s ==> %s" % (fname, target_fname)
                  if not os.path.exists(os.path.dirname(target_fname)):
                     os.makedirs(os.path.dirname(target_fname))
                  shutil.copy2(fname, target_fname)

                  # Check for subst
                  if is_subst_files:
                     self.runFileSubst(target_fname, grouped_files)
   #}

   def runMonitoredBuild(self, buildIds, interval):
      """
      Run indefinitely checking build.
      """
      last_file_details = {}   # Map from file to mod_timeList of tuples of (file, mod_time)
      last_conf_details = None # Tuple of file and mod_time

      while True:
         # -- CHECK FOR CONF FILE CHANGES --- #
         # if there are changes, reload the file
         new_conf_details = os.stat(self.confFile).st_mtime
         if new_conf_details != last_conf_details:
            last_conf_details = new_conf_details
            print "Changed conf detected, reloading..."
            self.loadConfig(self.confFile)

         # -- UPDATE ALL THE FILE GROUPS -- #
         # this catches any newly matched file names
         for pkg in self.packages:
            for buildKey in buildIds:
               cfg = pkg.getConfig(buildKey)
               if cfg is not None:
                  for fg in cfg.fileGroups.values():
                     fg.update()

         # Get full file list and all unique directories
         # - we monitor all of these for changes
         new_file_details = {}

         for buildKey in buildIds:
            file_list = self.getFullFileList(buildKey)
            for fname in file_list:
               file_dir = os.path.dirname(fname)
               if file_dir == "":  # handle special case of local directory
                  file_dir = "."
               if os.path.exists(file_dir):
                  if not new_file_details.has_key(file_dir):
                     new_file_details[file_dir] = os.stat(file_dir).st_mtime
               if not new_file_details.has_key(fname):
                  new_file_details[fname] = os.stat(fname).st_mtime

         # -- CHECK FOR CHANGES --- #
         if last_file_details != new_file_details:
            if len(last_file_details) != len(new_file_details):
               sym_diff = set(last_file_details.keys()).symmetric_difference(set(new_file_details.keys()))
               print "Found files changed [%s]..." % list(sym_diff)

            # Find the changes
            for (fname, mtime) in new_file_details.iteritems():
               old_time = last_file_details.get(fname, None)
               if (old_time != None) and (mtime != old_time):
                  print "file changed: ", fname

            last_file_details = new_file_details
            self.runBuild(buildIds)
            print "---- RE-BUILD DONE ---"

         # Wait
         time.sleep(interval)


   def getMergedFileGroup(self, buildKey):
      """
      Return a dict of all file groups with all files for the given build
      key.
      """
      file_map = {}
      for pkg in self.packages:
         cfg = pkg.getConfig(buildKey)
         if cfg is not None:
            for (fg_key, fg) in cfg.fileGroups.iteritems():
               file_map.setdefault(fg_key,[]).extend(fg.files)

      return file_map

   def getFullFileList(self, buildKey):
      file_list = []
      file_map = self.getMergedFileGroup(buildKey)
      for (fg_key, files) in file_map.iteritems():
         file_list.extend(files)
      return file_list

   @staticmethod
   def fileHasChanged(srcFname, targetFname, compareTime = True, compareSize = True):
      """
      helper to determine if a file may have changed and should be copied.
      returns true if the targetFname does not exist or does not match the size and/or
                   time of the source file.
      """
      if not os.path.exists(targetFname):
         return True
      src_stats = os.stat(srcFname)
      tgt_stats = os.stat(targetFname)
      if compareTime and (math.trunc(src_stats.st_mtime) != math.trunc(tgt_stats.st_mtime)):
         return True
      if compareSize and (src_stats.st_size != tgt_stats.st_size):
         return True

      return False


   @staticmethod
   def runFileSubst(fname, fileMap):
      """
      Perform an in place subst.  This code will load the given file, subst the content,
      and write it back out.

      @param fname: Full path to a file that must exist.  Replace the contents inside it.
      @param fileMap: Map from file key to list of files of that key.
      """
      print "Running subst on file: ", fname
      file_contents = open(fname, 'r').read()

      # Find CSS items
      css_files = fileMap.get("css_files", None)
      if css_files:
         css_contents = "<!-- CSS Files -->\n"
         for css_file in css_files:
            css_file = css_file.replace("\\", "/")
            css_contents += '<link rel="stylesheet" href="%s" type="text/css"/>\n' % css_file

         file_contents = re.sub("{%\s*?css_files\s*?%}", css_contents, file_contents)

      # Find JS items
      js_files = fileMap.get("js_files", None)
      if js_files:
         js_contents = "<!-- JS Files -->\n"
         for js_file in js_files:
            js_file = js_file.replace("\\", "/")
            js_contents += '<script type="text/javascript" src="%s"></script>\n' % js_file

         file_contents = re.sub("{%\s*?js_files\s*?%}", js_contents, file_contents)

      # Find Cache Related Items
      file_contents = re.sub("{%\s*?datetime\s*?%}", str(datetime.datetime.now()), file_contents )

      all_files_list = ""
      for (fg_key, files) in fileMap.iteritems():
         for file_path in files:
            file_path = os.path.normpath(file_path)
            file_path = file_path.replace("\\", "/")
            all_files_list += "%s\n" % file_path
      file_contents = re.sub("{%\s*?cache_files\s*?%}", all_files_list, file_contents)

      # Write out the file
      open(fname, 'w').write(file_contents)


   @staticmethod
   def compressJsFiles(compressionLevel, jsFileList, targetFname):
      """
      @param compressionLevel: amount of compression to use.
                              0 - no compression
                              1 - put everything in 1 file
                              2 - jmin
                              3 - magic (uglify)
                              4 - super magic (potentially change code)
      @param jsFileList: A list of js files that should be combined and compressed.
      @param targetFname: The destination file for the compression.
      """
      combined_data = ""
      for js_file in jsFileList:
         combined_data += open(js_file, 'r').read()
         combined_data += "\n"

      # Clamp compression to the largest amount we have available
      if compressionLevel > 3:
         compressed_data = 3

      compressed_data = ""
      if 3 == compressionLevel:
         uglify_js_path = os.path.expanduser('~/node_modules/.bin/uglifyjs')
         if not os.path.exists(uglify_js_path):
            print "Can't find uglifyjs [%s] dropping down to jsmin." % uglify_js_path
         p = subprocess.Popen([uglify_js_path, ], stdout = subprocess.PIPE, stdin = subprocess.PIPE)
         compressed_data = p.communicate(input=combined_data)[0]
      elif 2 == compressionLevel:
         import jsmin
         compressed_data = jsmin.jsmin(combined_data)
      elif 1 == compressionLevel:
         compressed_data = combined_data
      else:
         assert False, "Should not get here"

      # Write out the file
      open(targetFname, 'wb').write(compressed_data)


class Package(object):
   """ Represents an independent package that we need to pull
   together.  (ex. openlayers, app, etc)

   @ivar configs: Map from config key in the file to Configuration object.
   """
   def __init__(self, key):
      self.key = key
      self.configs = {}

   def getConfig(self, key):
      return self.configs.get(key, None)

   def config(self, configObj):
      """
      @param configObj: dictionary of configurations.  keyed by config name.
      """
      if configObj.has_key("id"):
         del configObj["id"]
      processed_one = False     # Flag to warn if in infinite loop
      rem_configs   = configObj # running list of remaining configs

      # process one configuration at a time
      # - (note: use while loop with remaining stack so we get a dep sort to handle the refs)
      # - if we have the reference, then process it.  ("ref" points to another config
      # - keep going until there are none left to process
      while(len(rem_configs) > 0):
         for (k, cfg) in copy.copy(rem_configs).items():
            ref_key = cfg.get("ref", None)
            if ((ref_key is None) or                # No ref key, so proc immediatley
               (self.configs.has_key(ref_key))):    # Ref key found, so we can process
               new_cfg_obj = Configuration(k)
               if ref_key is not None:              # If have ref, override with copy of base obj
                  new_cfg_obj = copy.deepcopy(self.configs.get(ref_key))
               new_cfg_obj.config(cfg)
               self.configs[k] = new_cfg_obj
               del rem_configs[k]                     # Rem since we have processed it
               processed_one = True

         if not processed_one:
            assert False, "Infinite loop in config reference in package: %s" % self.key


class Configuration(object):
   """
   A configuration for a given package.
   This is a set of files and other information that should be
   used when the given configuration is run.
   """
   def __init__(self, key):
      self.key        = key
      self.fileGroups = {}
      self.ref        = None

   def config(self, configObj):
      config_obj = copy.copy(configObj)
      if config_obj.has_key("ref"):
         self.ref = config_obj.get("ref")
         del config_obj["ref"]

      for (group_key, group_cfg) in config_obj.iteritems():
         # Try to get existing for case of "ref"
         file_group = self.fileGroups.get(group_key, None)
         if file_group is None:
            file_group = FileGroup(group_key)
         file_group.config(group_cfg)
         self.fileGroups[group_key] = file_group


class FileGroup(object):
   """
   Group of related files that come together.

   We keep track of a list of matchers so we can easily update the list later.
   note: we don't keep the "static" files separately because there may
   be an order dependency in the configuration file and we need to keep track of that.
   """
   def __init__(self, key):
      self.key   = key
      self.files = []  # List of paths to files that have been found

      # List of matcher items ("root dir", "pattern")
      #  OR  "file path"
      self.matchers = []

   def config(self, fgConfig):
      """
      configuration should be a list of file names or root/pattern dictionaries
      for looking up a group of files.
      """
      # We only setup the matchers here, update takes care of updating
      # the file list from those matchers
      for f_obj in fgConfig:
         # File string
         if isinstance(f_obj, types.StringTypes):
            if not os.path.exists(f_obj):
               print "ERROR: Can't find file: %s" % f_obj
               sys.exit(1)
            else:
               self.matchers.append(f_obj)
         elif isinstance(f_obj, dict) and \
              f_obj.has_key("root") and \
              f_obj.has_key("pattern"):
            self.matchers.append( (f_obj.get("root", ""), f_obj.get("pattern", "*")) )
         else:
            print "Invalid config in file group: %s" % self.key

      # Update so we get the file list set accurately
      self.update()

   def update(self):
      """
      Update the file list based on current files in the directory.
      (only changes if there is a root and pattern)
      """
      file_list = []

      for matcher in self.matchers:
         if isinstance(matcher, types.StringTypes):
            fname = os.path.normpath(matcher)
            file_list.append(fname)
         else:
            (root_dir, pattern) = matcher
            file_list.extend(matchFiles(root_dir, pattern))

      # Now prune out duplicates
      self.files = []
      for fname in file_list:
         if fname not in self.files:
            self.files.append(fname)



class Build(object):
   """
   Object wrapping a build configuration.

   @type hidden: boolean
   @ivar hidden: If True this build should not be included in the list of all
                 builds when run without specifying a build target.

   @ivar compressJsLevel: 0 - no compression
                          1 - put everything in 1 file
                          2 - jmin
                          3 - magic (uglify)
                          4 - super magic (potentially change code)
   """
   def __init__(self, key):
      self.key       = key
      self.targetDir = ""
      self.hidden    = False

      self.compressJsLevel      = 0
      self.compressedJsFilename = ''

   def config(self, buildConfig):
      self.targetDir = buildConfig.get("target_dir")
      self.hidden    = buildConfig.get("hidden", False)

      js_compression = buildConfig.get("js_compression", None)
      if js_compression is not None:
         self.compressJsLevel      = js_compression.get("level", self.compressJsLevel)
         self.compressedJsFilename = js_compression.get("filename", 'compressed_app.js')


def matchFiles(rootDir, pattern, ignoreDirs = [".svn",".sass-cache",]):
   """
   ex: matchFiles("/home/allenb", "*.js")
   """
   matches = []
   for root, dirnames, filenames in os.walk(rootDir):
      for bad_dir_name in ignoreDirs:
         if bad_dir_name in dirnames:
            dirnames.remove(bad_dir_name)
      for filename in fnmatch.filter(filenames, pattern):
         fname = os.path.normpath(pj(root, filename))
         matches.append(fname)
   return matches


def generateBuildNumber(dir = None):
   if dir is None:
      dir = ""

   revision_exp = re.compile("^Revision: (\d+)$")

   revision = None
   pipe = subprocess.Popen("svn info %s" % dir, shell = True, stdout = subprocess.PIPE).stdout

   for line in pipe.readlines():
      match = revision_exp.match(line)
      if match is not None:
         revision = int(match.group(1))
         break

   return revision



if __name__ == '__main__':
   main()
