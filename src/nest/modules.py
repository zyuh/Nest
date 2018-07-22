import os
import re
import sys
import fnmatch
import inspect
import importlib
import warnings
import subprocess
from types import ModuleType
from typing import Any, List, Dict, Iterator, Callable, Optional
from difflib import SequenceMatcher
from datetime import datetime
from argparse import Namespace as BaseNamespace
from inspect import formatannotation as format_anno

from nest import utils as U
from nest.logger import exception
from nest.settings import settings


class Context(BaseNamespace):
    """Helper class for storing module context.
    """

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def __setitem__(self, key: str, val: str) -> Any:
        return setattr(self, key, val)

    def __iter__(self) -> Iterator:
        return iter(self.__dict__.items())

    def items(self) -> Iterator:
        return self.__dict__.items()

    def keys(self) -> Iterator:
        return self.__dict__.keys()

    def values(self) -> Iterator:
        return self.__dict__.values()

    def clear(self):
        self.__dict__.clear()


class NestModule(object):
    """Base Nest module class.
    """

    __slots__ = ('__name__', 'func', 'sig', 'meta', 'params')

    def __init__(self, func: Callable, meta: Dict[str, object], params: dict = {}) -> None:
        # module func
        self.func = func
        self.__name__ = func.__name__
        # module signature
        self.sig = inspect.signature(func)
        # meta information
        self.meta = U.merge_dict(dict(), meta, union=True)
        # record module params
        self.params = U.merge_dict(dict(), params, union=True)
        # init module context
        for k, v in self.sig.parameters.items():
            if k =='ctx' and issubclass(v.annotation, Context):
                self.params[k] = v.annotation()
            break
        # check module
        self._check_definition()

    def _check_definition(self) -> None:
        """Raise errors if the module definition is invalid.
        """

        for v in self.sig.parameters.values():
            # type of parameters must be annotated
            if v.annotation is inspect.Parameter.empty:
                raise TypeError('The param "%s" of Nest module "%s" is not explicitly annotated.' % (v, self.__name__))
            # type of defaults must match annotations
            if v.default is not inspect.Parameter.empty and not U.is_annotation_matched(v.default, v.annotation):
                raise TypeError('The param "%s" of Nest module "%s" has an incompatible default value of type "%s".' %
                    (v, self.__name__, format_anno(type(v.default))))

        # type of returns must be annotated
        if self.sig.return_annotation is inspect.Parameter.empty:
            raise TypeError('The returns of Nest module "%s" is not explicitly annotated.' % self.__name__)

        # important meta data must be provided
        if getattr(self, '__doc__', None) is None:
            raise KeyError('Documentation of module "%s" is missing.' % self.__name__)
 
    def _check_params(self, params: dict) -> None:
        """Raise errors if invalid params are provided to the Nest module.

        Parameters:
            params: 
                The provided params
        """

        unexpected_params = ', '.join(set(params.keys()) - set(self.sig.parameters.keys()))
        if len(unexpected_params) > 0:
            raise TypeError('Unexpected param(s) "%s" for Nest module: \n%s' % \
                (unexpected_params, self))

        for k, v in self.sig.parameters.items():
            resolved = params.get(k)
            if resolved is None:
                if v.default is inspect.Parameter.empty:
                    raise KeyError('The required param "%s" of Nest module "%s" is missing.' % \
                        (v, self.__name__))
            elif not U.is_annotation_matched(resolved, v.annotation):
                if issubclass(type(resolved), NestModule):
                    detailed_msg = 'The param "%s" of Nest module "%s" should be type of "%s". Got \n%s\n' + \
                    'Please check if some important params of Nest module "%s" have been forgotten in use.'
                    raise TypeError(detailed_msg % \
                        (k, self.__name__, format_anno(v.annotation), U.indent_text(str(resolved), 4), resolved.__name__))
                else:
                    raise TypeError('The param "%s" of Nest module "%s" should be type of "%s". Got "%s".' % \
                        (k, self.__name__, format_anno(v.annotation), resolved))

    def _check_returns(self, returns: Any) -> None:
        """Raise errors if invalid returns are generated by the Nest module.

        Parameters:
            returns: 
                The generated returns
        """

        if not U.is_annotation_matched(returns, self.sig.return_annotation):
            raise TypeError('The returns of Nest module "%s" should be type of "%s". Got "%s".' % \
                (self.__name__, format_anno(self.sig.return_annotation), returns))

    def __call__(self, *args, **kwargs):
        # handle positional params
        num_args = len(args)
        if num_args > 0:
            # positional params should not be optional or resolved
            expected_param_names = [k for k, v in self.sig.parameters.items()
                                    if not k in self.params.keys() and v.default is inspect.Parameter.empty]
            num_expected_params = len(expected_param_names)
            if num_args != num_expected_params:
                raise TypeError('Nest module "%s" expects %d positional param(s) "%s". Got "%s".' %
                                (self.__name__, num_expected_params, ', '.join(expected_param_names), ', '.join([str(v) for v in args])))
            for idx, val in enumerate(args):
                key = expected_param_names[idx]
                if key in kwargs.keys():
                    raise TypeError('Nest module "%s" got multiple values for param "%s".' % (self.__name__, key))
                else:
                    kwargs[key] = val
        
        # resolve params
        resolved_params = dict()
        U.merge_dict(resolved_params, self.params, union=True)
        U.merge_dict(resolved_params, kwargs, union=True)

        if resolved_params.pop('delay_resolve', None):
            try:
                self._check_params(resolved_params)
                returns = self.func(**resolved_params)
            except KeyError as exc_info:
                if 'Nest module' in str(exc_info):
                    # wait for next call
                    return self.clone(resolved_params)
                else:
                    raise
        else:
            # parameters must be fulfilled
            self._check_params(resolved_params)
            returns = self.func(**resolved_params)
        # check returns
        self._check_returns(returns)
        return returns

    def __str__(self) -> str:
        param_string = ', \n'.join(['[✓] ' + str(v) 
            if k in self.params.keys() else '    ' + str(v)
            for k, v in self.sig.parameters.items()])
        return_string = ' -> ' + format_anno(self.sig.return_annotation)
        return self.__name__ + '(\n' + param_string + ')' + return_string

    def __repr__(self) -> str:
        return "nest.modules['%s']" % self.__name__

    def clone(self, params: dict = {}) -> Callable:
        """Clone the Nest module.

        Parameters:
            params:
                Module parameters
        """

        return type(self)(self.func, self.meta, params)


class ModuleManager(object):
    """Helper class for easy access to Nest modules.
    """

    def __init__(self) -> None:
        self.namespaces = dict()
        self.py_modules = dict()
        self.nest_modules = dict()
        self.update_timestamp = 0.0
        self.namespace_regex = re.compile(r'^[a-z][a-z0-9\_]*\Z')
        # get available namespaces
        self._update_namespaces()
        # import syntax
        self._add_module_finder()

    @staticmethod
    def _format_namespace(src: str) -> str:
        """Format namespace.

        Parameters:
            src:
                The original namespace
        
        Returns:
            Formatted namespace.
        """

        return src.lower().replace('-', '_').replace('.', '_')

    @staticmethod
    def _register(*args, **kwargs) -> Callable:
        """Decorator for Nest modules registration.

        Parameters:
            ignored:
                Ignore the module

            module meta information which could be utilized by CLI and UI. For example:
            author: 
                Module author(s), e.g., 'Zhou, Yanzhao'
            version: 
                Module version, e.g., '1.2.0'
            backend:
                Module backend, e.g., 'pytorch'
            tags: 
                Searchable tags, e.g., ['loss', 'cuda_only']
            etc.
        """

        # ignore the Nest module (could be used for debuging)
        if kwargs.pop('ignored', False):
            return lambda x: x

        # use the rest of kwargs to update metadata
        frame = inspect.stack()[1]
        current_py_module = inspect.getmodule(frame[0])
        nest_meta = U.merge_dict(getattr(current_py_module, '__nest_meta__', dict()), kwargs, union=True)
        if current_py_module is not None:
            setattr(current_py_module, '__nest_meta__', nest_meta)

        def create_module(func):
            # append meta to doc
            doc = (func.__doc__ + '\n' + (U.yaml_format(nest_meta) if len(nest_meta) > 0 else '')) \
                if isinstance(func.__doc__, str) else None
            return type('NestModule', (NestModule,), dict(__slots__=(), __doc__=doc))(func, nest_meta)

        if len(args) == 1 and inspect.isfunction(args[0]):
            return create_module(args[0])
        else:
            return create_module
        
    @staticmethod
    def _import_nest_modules_from_py_module(
        namespace: str, 
        py_module: object, 
        nest_modules: Dict[str, object]) -> bool:
        """Import registered Nest modules from a given python module.

        Parameters:
            namespace:
                A namespace that is used to avoid name conflicts
            py_module:
                The python module
            nest_modules:
                The dict for storing Nest modules
        
        Returns:
            The id of imported Nest modules
        """

        imported_ids = []
        # search for Nest modules
        for key, val in py_module.__dict__.items():
            module_id = U.encode_id(namespace, key)
            if not key.startswith('_') and type(val).__name__ == 'NestModule':
                if module_id in nest_modules.keys():
                    U.alert_msg('There are duplicate "%s" modules under namespace "%s".' % \
                        (key, namespace))
                else:
                    nest_modules[module_id] = val
                    imported_ids.append(module_id) 
        return imported_ids

    @staticmethod
    def _import_nest_modules_from_file(
        path: str, 
        namespace: str,
        py_modules: Dict[str, float], 
        nest_modules: Dict[str, object], 
        meta: Dict[str, object] = dict()) -> None:
        """Import registered Nest modules form a given file.

        Parameters:
            path: 
                The path to the file
            namespace:
                A namespace that is used to avoid name conflicts
            py_modules:
                The dict for storing python modules information
            nest_modules:
                The dict for storing Nest modules
            meta:
                Global meta information
        """

        py_module_name = os.path.basename(path).split('.')[0]
        py_module_id = U.encode_id(namespace, py_module_name)
        timestamp = os.path.getmtime(path)
        # check whether the python module have already been imported
        is_reload = False
        if py_module_id in py_modules.keys():
            if timestamp <= py_modules[py_module_id][0]:
                # skip
                return
            else:
                is_reload = True
        # import the python module
        # note that a python module could contain multiple Nest modules.
        ref_id = 'nest.' + namespace + '.' + py_module_name
        spec = importlib.util.spec_from_file_location(ref_id, path)
        if spec is not None:
            py_module = importlib.util.module_from_spec(spec)
            py_module.__nest_meta__ = U.merge_dict(dict(), meta, union=True)
            # no need to bind global requirements to individual Nest modules.
            requirements = py_module.__nest_meta__.pop('requirements', None)
            if requirements is not None:
                requirements = [dict(url=v, tool='pip') if isinstance(v, str) else v for v in requirements]
            sys.modules[ref_id] = py_module
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    spec.loader.exec_module(py_module)
            except Exception as exc_info:
                # helper function
                def find_requirement(name):
                    if isinstance(requirements, list) and len(requirements) > 0:
                        scores = [(SequenceMatcher(None, name, v['url']).ratio(), v) for v in requirements]
                        return max(scores, key=lambda x: x[0])
                     
                # install tip
                tip = ''
                if (type(exc_info) is ImportError or type(exc_info) is ModuleNotFoundError) and exc_info.name is not None:
                    match = find_requirement(exc_info.name)
                    if match and match[0] > settings['INSTALL_TIP_THRESHOLD']:
                        tip = 'Try to execute "%s install %s" to install the missing dependency.' % \
                            (match[1]['tool'], match[1]['url'])

                exc_info = str(exc_info)
                exc_info = exc_info if exc_info.endswith('.') else exc_info + '.'    
                U.alert_msg('%s The package "%s" under namespace "%s" could not be imported. %s' %
                    (exc_info, py_module_name, namespace, tip))
            else:
                # remove old Nest modules
                if is_reload:
                    for key in py_modules[py_module_id][1]:
                        if key in nest_modules.keys():
                            del nest_modules[key]
                # import all Nest modules within the python module
                imported_ids = ModuleManager._import_nest_modules_from_py_module(namespace, py_module, nest_modules)
                if len(imported_ids) > 0:
                    # record modified time, id, and spec of imported Nest modules
                    py_modules[py_module_id] = (timestamp, imported_ids, py_module.__spec__)

    @staticmethod
    def _import_nest_modules_from_dir(
        path: str, 
        namespace: str,
        py_modules: Dict[str, float],
        nest_modules: Dict[str, object],
        meta: Dict[str, object] = dict()) -> None:
        """Import registered Nest modules form a given directory.

        Parameters:
            path: 
                The path to the directory
            namespace:
                A namespace that is used to avoid name conflicts
            py_modules:
                The dict for storing modified timestamp of python modules
            nest_modules:
                The dict for storing Nest modules
            meta:
                Global meta information
        
        Returns:
            The Nest modules
            The set of python modules
        """

        for entry in os.listdir(path):
            file_path = os.path.join(path, entry)
            if entry.endswith('.py') and os.path.isfile(file_path):
                ModuleManager._import_nest_modules_from_file(file_path, namespace, py_modules, nest_modules, meta)

    @staticmethod
    def _fetch_nest_modules_from_url(url: str, dst: str) -> None:
        """Fetch and unzip Nest modules from url.

        Parameters:
            url:
                URL of the zip file or git repo
            dst:
                Save dir path
        """

        def _hook(count, block_size, total_size):
            size = float(count * block_size) / (1024.0 * 1024.0)
            total_size = float(total_size / (1024.0 * 1024.0))
            if total_size > 0:
                size = min(size, total_size)
                percent = 100.0 * size / total_size
                sys.stdout.write("\rFetching...%d%%, %.2f MB / %.2f MB" % (percent, size, total_size))
            else:
                sys.stdout.write("\rFetching...%.2f MB" % size)
            sys.stdout.flush()
        
        # extract
        if url.endswith('zip'):
            import random
            import string
            import zipfile
            from urllib import request, error
            
            cache_name = ''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(6)) + '.cache'
            cache_path = os.path.join(dst, cache_name)

            try:
                # download
                request.urlretrieve(url, cache_path, _hook)
                sys.stdout.write('\n')
                # unzip
                with zipfile.ZipFile(cache_path, 'r') as f:
                    file_list = f.namelist()
                    namespaces = set([v.split('/')[0] for v in file_list])
                    members = [v for v in file_list if '/' in v]
                    f.extractall(dst, members)
                return namespaces
            except error.URLError as exc_info:
                U.alert_msg('Could not fetch "%s". %s' % (url, exc_info))
                return []
            except Exception as exc_info:
                U.alert_msg('Error occurs during extraction. %s' % exc_info)
                return []
            finally:
                # remove cache
                if os.path.exists(cache_path):
                    os.remove(cache_path)
        elif url.endswith('.git'):
            try:
                subprocess.check_call(['git', 'clone'] + url.split())
                return [url[url.rfind('/')+1: -4]]
            except subprocess.CalledProcessError as exc_info:
                U.alert_msg('Failed to clone "%s".' % url)
            return []
        else:
            raise NotImplementedError('Only supports zip file and git repo for now. Got "%s".' % url)

    @staticmethod
    def _install_namespaces_from_url(url: str, namespace: Optional[str] = None) -> None:
        """Install namespaces from url.

        Parameters:
            url:
                URL of the zip file or git repo
            namespace:
                Specified namespace
        """
        # pre-process short URL
        if url.startswith('github@'):
            m = re.match(r'^github@([\w\-\_]+)/([\w\-\_]+)(:[\w\-\_]+)*$', url)
            repo = m.group(1) + '/' + m.group(2)
            branch = m.group(3) or ':master'
            url = '-b %s https://github.com/%s.git' % (branch[1:], repo)
        elif url.startswith('gitlab@'):
            m = re.match(r'^gitlab@([\w\-\_]+)/([\w\-\_]+)(:[\w\-\_]+)*$', url)
            repo = m.group(1) + '/' + m.group(2)
            branch = m.group(3) or ':master'
            url = '-b %s https://gitlab.com/%s.git' % (branch[1:], repo)
        elif url.startswith('bitbucket@'):
            m = re.match(r'^bitbucket@([\w\-\_]+)/([\w\-\_]+)(:[\w\-\_]+)*$', url)
            repo = m.group(1) + '/' + m.group(2)
            branch = m.group(3) or ':master'
            url = '-b %s https://bitbucket.org/%s.git' % (branch[1:], repo)
        elif url.startswith('file@'):
            path = url[5:]
            url = 'file:///' + os.path.abspath(path)

        for dirname in ModuleManager._fetch_nest_modules_from_url(url, './'):
            module_path = os.path.join('./', dirname)
            ModuleManager._install_namespaces_from_path(module_path, namespace)
            # parse config
            meta_path = os.path.join(module_path, settings['NAMESPACE_CONFIG_FILENAME'])
            meta = U.load_yaml(meta_path)[0] if os.path.exists(meta_path) else dict()
            if settings['AUTO_INSTALL_REQUIREMENTS']:
                # auto install deps
                for dep in meta.get('requirements', []):
                    # helper function
                    def install_dep(url, tool):
                        # filter deps
                        if re.match(r'^[a-zA-Z0-9<=>.-]+$', dep):
                            try:
                                subprocess.check_call([sys.executable, '-m', tool, 'install', dep])
                            except subprocess.CalledProcessError:
                                U.alert_msg('Failed to install "%s" for "%s". Please manually install it.' % (dep, dirname))
                    if isinstance(dep, str):
                        # use pip by default 
                        install_dep(dep, 'pip')
                    elif isinstance(dep, dict) and 'url' in dep and 'tool' in dep:
                        install_dep(dep['url'], dep['tool'])
                    else:
                        U.alert_msg('Invalid install requirement "%s".' % dep)

    @staticmethod
    def _install_namespaces_from_path(path: str, namespace: Optional[str] = None) -> None:
        """Install namespaces from path.

        Parameters:
            path:
                Path to the directory
            namespace:
                Specified namespace
        """
        
        path = os.path.abspath(path)
        namespace = namespace or ModuleManager._format_namespace(os.path.basename(path))
        search_paths = settings['SEARCH_PATHS']
        for k, v in search_paths.items():
            if namespace == k:
                U.alert_msg('Namespace "%s" is already bound to the path "%s".' % (k, v))
                return
            if path == v:
                U.alert_msg('"%s" is already installed under the namespace "%s".' % (v, k))
                return
        search_paths[namespace] = path
        settings['SEARCH_PATHS'] = search_paths
        settings.save()

    @staticmethod
    def _remove_namespaces_from_path(src: str) -> Optional[str]:
        """Remove namespaces from path.

        Parameters:
            src:
                Namespace or path
        """
        
        if os.path.isdir(src):
            path, namespace = os.path.abspath(src), None
        else:
            path, namespace = None, src

        delete_key = None
        search_paths = settings['SEARCH_PATHS']
        for k, v in search_paths.items():
            if namespace == k:
                delete_key = k
                break
            if path == v:
                delete_key = k
                break

        if delete_key is None:
            if namespace:
                U.alert_msg('The namespace "%s" is not installed.' % namespace)
            if path:
                U.alert_msg('The path "%s" is not installed.' % path)
        else:
            path = search_paths.pop(delete_key)
            settings['SEARCH_PATHS'] = search_paths
            settings.save()
            return path
                
    @staticmethod
    def _pack_namespaces(srcs: List[str], dst: str) -> List[str]:
        """Pack namespaces to a zip file.

        Parameters:
            srcs:
                Path to the namespaces
            dst:
                Save path for the resulting zip file
        
        Returns:
            Archived files
        """

        import zipfile

        save_list = dict()
        for src in srcs:
            namespace = os.path.basename(os.path.normpath(src))
            # helper function
            def check_extension(filename):
                splits = filename.split('.')
                if len(splits) > 1:
                    # Python file, YAML config, Plain text, Markdown file, Image, and IPython Notebook
                    return splits[-1] in ['py', 'yml', 'txt', 'md', 'jpg', 'png', 'gif', 'ipynb']
                else:
                    return True
            # scan files
            file_list = []
            for root, dirs, files in os.walk(src):
                dirs[:] = [v for v in dirs if not (v[0] == '.' or v.startswith('__'))]
                file_list += [os.path.join(root, v) for v in files if not v[0] == '.' and check_extension(v)]
            save_list[namespace] = file_list

            # save to the zip file
            with zipfile.ZipFile(dst, 'w', zipfile.ZIP_DEFLATED) as f:
                for v in file_list:
                    f.write(v, os.path.join(namespace, os.path.relpath(v, src)))

        return save_list
        
    def _add_module_finder(self) -> None:
        """Add a custom finder to support Nest module import syntax.
        """

        module_manager = self

        class NamespaceLoader(importlib.abc.Loader):
            def create_module(self, spec):
                _, namespace = spec.name.split('.')
                module = ModuleType(spec.name)
                module_manager._update_namespaces()
                meta = module_manager.namespaces.get(namespace)
                module.__path__ = [meta['module_path']] if meta else []
                return module

            def exec_module(self, module):
                pass

        class NestModuleFinder(importlib.abc.MetaPathFinder):
            def __init__(self):
                super(NestModuleFinder, self).__init__()
                self.reserved_namespaces = [
                    v[:-3] for v in os.listdir(os.path.dirname(os.path.realpath(__file__))) if v.endswith('.py')]

            def find_spec(self, fullname, path, target=None):
                if fullname.startswith('nest.'):
                    name = fullname.split('.')
                    if len(name) == 2:
                        if not name[1] in self.reserved_namespaces:
                            return importlib.machinery.ModuleSpec(fullname, NamespaceLoader())

        sys.meta_path.insert(0, NestModuleFinder())

    def _update_namespaces(self) -> None:
        """Get the available namespaces.
        """

        # user defined search paths
        dir_list = set()
        self.namespaces = dict()
        for k, v in settings['SEARCH_PATHS'].items():
            if os.path.isdir(v):
                meta_path = os.path.join(v, settings['NAMESPACE_CONFIG_FILENAME'])
                meta = U.load_yaml(meta_path)[0] if os.path.exists(meta_path) else dict()
                meta['module_path'] = os.path.abspath(os.path.join(v, meta.get('module_path', './')))
                if os.path.isdir(meta['module_path']):
                    self.namespaces[k] = meta
                    dir_list.add(meta['module_path'])
                else:
                    U.alert_msg('Namespace "%s" has an invalid module path "%s".' % (k, meta['module_path']))

        # current path
        current_path = os.path.abspath(os.curdir)
        if not current_path in dir_list:
            self.namespaces['main'] = dict(module_path=current_path)
        
    def _update_modules(self) -> None:
        """Automatically import all available Nest modules.
        """

        timestamp = datetime.now().timestamp()
        if timestamp - self.update_timestamp > settings['UPDATE_INTERVAL']:
            for namespace, meta in self.namespaces.items():
                importlib.import_module('nest.' + namespace)
                ModuleManager._import_nest_modules_from_dir(meta['module_path'], namespace, self.py_modules, self.nest_modules, meta)
            self.update_timestamp = timestamp

    def __iter__(self) -> Iterator:
        """Iterator for Nest modules.

        Returns:
            The Nest module iterator
        """

        self._update_modules()
        return iter(self.nest_modules.items())

    def __len__(self):
        """Number of Nest modules

        Returns:
            The number of Nest modules
        """

        self._update_modules()
        return len(self.nest_modules)

    def _ipython_key_completions_(self) -> List[str]:
        """Support IPython key completion.

        Returns:
            A list of module ids
        """
        self._update_modules()
        return list(self.nest_modules.keys())

    def __dir__(self) -> List[str]:
        """Support IDE auto-completion

        Returns:
            A list of module names
        """

        self._update_modules()
        return list([U.decode_id(uid)[1] for uid in self.nest_modules.keys()])

    @exception
    def __getattr__(self, key: str) -> object:
        """Get a Nest module by name.

        Parameters:
            key: 
                Name of the Nest module
        
        Returns:
            The Nest module
        """
        
        self._update_modules()
        matches = []
        for uid in self.nest_modules.keys():
            _, module_key = U.decode_id(uid)
            if key == module_key:
                matches.append(uid)
        if len(matches) == 0:
            raise KeyError('Could not find the Nest module "%s".' % key)
        elif len(matches) > 1:
            warnings.warn('Multiple Nest modules with this name have been found. \n'
            'The returned module is "%s", but you can use nest.modules[regex] to specify others: \n%s' %
            (matches[0], '\n'.join(['[%d] %s %s' % (k, v, self.nest_modules[v].sig) for k, v in enumerate(matches)])))

        return self.nest_modules[matches[0]].clone()

    @exception
    def __getitem__(self, key: str) -> object:
        """Get a Nest module by a query string.
        
        There are three match modes:
        1. Exact match if the query string starts with '$':
            E.g., nest.modules['$nest/optimizer']
        2. Regex match if the query string starts with 'r/':
            E.g., nest.modules['r/.*optim\w+']
        3. Wildcard match if otherwise:
            E.g., nest.modules['optim*er'].
            Note that a wildcard is automatically added to the beginning of the string.

        Parameters:
            key: 
                The query string
        
        Returns:
            The Nest module
        """

        self._update_modules()
        if isinstance(key, str):
            if key.startswith('$'):
                # exact match
                key = key[1:]
                if key in self.nest_modules.keys():
                    return self.nest_modules[key].clone()
                else:
                    raise KeyError('Could not find Nest module "%s".' % key)
            elif key.startswith('r/'):
                # regex match
                key = key[2:]
                r = re.compile(key)
                matches = list(filter(r.match, self.nest_modules.keys()))
                if len(matches) == 0:
                    raise KeyError('Could not find a Nest module matches regex "%s".' % key)
                elif len(matches) > 1:
                    warnings.warn('Multiple Nest modules match the given regex have been found. \n'
                        'The returned module is "%s", but you can adjust regex to specify others: \n%s' %
                        (matches[0], '\n'.join(['[%d] %s %s' % (k, v, self.nest_modules[v].sig) for k, v in enumerate(matches)])))
                return self.nest_modules[matches[0]].clone()
            else:
                # wildcard match
                if not key[0] == '*':
                    key = '*' + key
                matches = fnmatch.filter(self.nest_modules.keys(), key)
                if len(matches) == 0:
                    raise KeyError('Could not find a Nest module matches query "%s".' % key)
                elif len(matches) > 1:
                    warnings.warn('Multiple Nest modules match the given regex have been found. \n'
                        'The returned module is "%s", but you can adjust regex to specify others: \n%s' %
                        (matches[0], '\n'.join(['[%d] %s %s' % (k, v, self.nest_modules[v].sig) for k, v in enumerate(matches)])))
                return self.nest_modules[matches[0]].clone()
        else:
            raise NotImplementedError
    
    def __repr__(self) -> str:
        return 'nest.modules'
    
    def __str__(self) -> str:
        num = self.__len__()
        if num == 0:
            return 'No Nest module found.'
        elif num == 1: 
            return 'Found 1 Nest module.'
        else:
            return '%d Nest modules are availble.' % num

# global manager
module_manager = ModuleManager()
