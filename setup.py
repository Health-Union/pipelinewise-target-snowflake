#!/usr/bin/env python

from setuptools import setup

with open('README.md') as f:
    long_description = f.read()

setup(name="pipelinewise-target-snowflake",
      version="1.9.0",
      description="Singer.io target for loading data to Snowflake - PipelineWise compatible",
      long_description=long_description,
      long_description_content_type='text/markdown',
      author="TransferWise",
      url='https://github.com/transferwise/pipelinewise-target-snowflake',
      classifiers=[
          'License :: OSI Approved :: Apache Software License',
          'Programming Language :: Python :: 3 :: Only'
      ],
      py_modules=["target_snowflake"],
      install_requires=[
          'idna==2.7',
          'pipelinewise-singer-python==1.*',
          'snowflake-connector-python==2.3.6',
          'inflection==0.3.1',
          'joblib==0.16.0',
          'python-dateutil==2.8.1',
          'snowflake-ingest==1.0.2'
      ],
      extras_require={
          "test": [
              "nose==1.3.7",
              "mock==3.0.5",
              "pylint==2.4.2",
              "python-dotenv==0.14.0",
              "pytest"
          ]
      },
      entry_points="""
          [console_scripts]
          target-snowflake=target_snowflake:main
      """,
      packages=["target_snowflake"],
      )
