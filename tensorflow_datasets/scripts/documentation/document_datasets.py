# coding=utf-8
# Copyright 2020 The TensorFlow Datasets Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Util to generate the dataset documentation content.

Used by tensorflow_datasets/scripts/documentation/build_catalog.py

"""

from concurrent import futures
import functools
import os
from typing import Any, Iterator, List, Optional, Tuple, Type

import dataclasses

import mako.lookup
import tensorflow.compat.v2 as tf
import tensorflow_datasets as tfds
from tensorflow_datasets.scripts.documentation import dataset_markdown_builder
from tensorflow_datasets.scripts.documentation import doc_utils
import tqdm

_WORKER_COUNT_DATASETS = 50
_WORKER_COUNT_CONFIGS = 20

_TEST_SECTION = 'testing'
# We do not document datasets in those sections
_FILTERED_SECTIONS = frozenset({
    'testing',
})

# WmtTranslate: The raw wmt can only be instantiated with the config kwargs
# TODO(tfds): Document image_label_folder datasets in a separate section
BUILDER_BLACKLIST = ['wmt_translate']


@dataclasses.dataclass(eq=False, frozen=True)
class BuilderDocumentation:
  """Documentation output of a single builder.

  Attributes:
    name: Documentation page name (e.g. `mnist`)
    content: Documentation content
    section: Documentation section (e.g `text`, `image`,...)
    is_manual: Whether the dataset require manual download
    is_nightly: Whether the dataset was recently added in `tfds-nightly`
  """
  name: str
  content: str
  section: str
  is_manual: bool
  is_nightly: bool


@tfds.core.utils.memoize()
def get_mako_template(tmpl_name):
  """Returns mako.lookup.Template object to use to render documentation.

  Args:
    tmpl_name: string, name of template to load.

  Returns:
    mako 'Template' instance that can be rendered.
  """
  tmpl_path = tfds.core.utils.tfds_path(
      'scripts/documentation/templates/%s.mako.md' % tmpl_name)
  with tf.io.gfile.GFile(os.fspath(tmpl_path), 'r') as tmpl_f:
    tmpl_content = tmpl_f.read()
  return mako.lookup.Template(tmpl_content, default_filters=['str', 'trim'])


def _load_builder(
    builder_cls: Type[tfds.core.DatasetBuilder],
) -> Tuple[tfds.core.DatasetBuilder, List[tfds.core.DatasetBuilder]]:
  """Load the builder to document.

  Args:
    builder_cls: Builder to load

  Returns:
    builder: Main builder instance
    config_builders: Additional builders (one of each configs)
  """
  if builder_cls.BUILDER_CONFIGS:  # Builder with configs

    def get_config_builder(config) -> tfds.core.DatasetBuilder:
      return tfds.builder(builder_cls.name, config=config)

    with futures.ThreadPoolExecutor(max_workers=_WORKER_COUNT_CONFIGS) as tpool:
      config_builders = list(
          tpool.map(get_config_builder, builder_cls.BUILDER_CONFIGS),
      )
    return config_builders[0], config_builders
  else:  # Builder without configs
    return builder_cls(), []  # pytype: disable=not-instantiable


def _get_section(builder_cls: Type[tfds.core.DatasetBuilder]) -> str:
  """Returns the section associated with the builder."""
  module_parts = builder_cls.__module__.split('.')
  if module_parts[0] != 'tensorflow_datasets':
    raise AssertionError(f'Unexpected builder {builder_cls}: module')
  if 'testing' in module_parts:
    return _TEST_SECTION
  _, category, *_ = module_parts  # tfds.<category>.xyz
  return category


def _document_single_builder(
    name: str, **kwargs: Any,
) -> Optional[BuilderDocumentation]:
  """Doc string for a single builder, with or without configs."""
  with tfds.core.utils.try_reraise(f'Error for {name}: '):
    return _document_single_builder_inner(name, **kwargs)


def _document_single_builder_inner(
    name: str,
    visu_doc_util: doc_utils.VisualizationDocUtil,
    df_doc_util: doc_utils.DataframeDocUtil,
    nightly_doc_util: doc_utils.NightlyDocUtil,
) -> Optional[BuilderDocumentation]:
  """Doc string for a single builder, with or without configs."""
  builder_cls = tfds.builder_cls(name)
  section = _get_section(builder_cls)
  if section in _FILTERED_SECTIONS:
    return None

  tqdm.tqdm.write(f'Document builder {name}...')
  builder, config_builders = _load_builder(builder_cls)

  out_str = dataset_markdown_builder.get_markdown_string(
      builder=builder,
      config_builders=config_builders,
      visu_doc_util=visu_doc_util,
      df_doc_util=df_doc_util,
      nightly_doc_util=nightly_doc_util,
  )
  schema_org_tmpl = get_mako_template('schema_org')
  schema_org_out_str = schema_org_tmpl.render_unicode(
      builder=builder,
      config_builders=config_builders,
      visu_doc_util=visu_doc_util,
  ).strip()
  out_str = schema_org_out_str + '\n' + out_str
  return BuilderDocumentation(
      name=name,
      content=out_str,
      section=section,
      is_manual=bool(builder_cls.MANUAL_DOWNLOAD_INSTRUCTIONS),
      is_nightly=nightly_doc_util.is_builder_nightly(name),
  )


def iter_documentation_builders(
    datasets: Optional[List[str]] = None,
) -> Iterator[BuilderDocumentation]:
  """Create dataset documentation string for given datasets.

  Args:
    datasets: list of datasets for which to create documentation.
              If None, then all available datasets will be used.

  Yields:
    builder_documetation: The documentation information for each builder
  """
  print('Retrieving the list of builders...')
  if not datasets:
    datasets = sorted([
        name
        for name in tfds.list_builders(with_community_datasets=False)
        if name not in BUILDER_BLACKLIST
    ])

  document_single_builder_fn = functools.partial(
      _document_single_builder,
      visu_doc_util=doc_utils.VisualizationDocUtil(),
      df_doc_util=doc_utils.DataframeDocUtil(),
      nightly_doc_util=doc_utils.NightlyDocUtil(),
  )

  # Document all builders
  print(f'Document {len(datasets)} builders...')
  with futures.ThreadPoolExecutor(max_workers=_WORKER_COUNT_DATASETS) as tpool:
    tasks = [
        tpool.submit(document_single_builder_fn, name) for name in datasets
    ]
    for future in tqdm.tqdm(futures.as_completed(tasks), total=len(tasks)):
      builder_doc = future.result()
      if builder_doc is None:  # Builder filtered
        continue
      else:
        tqdm.tqdm.write(f'Documentation generated for {builder_doc.name}...')
        yield builder_doc
  print('All builder documentations generated!')
