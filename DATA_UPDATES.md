Adding new data release
=======================

#### Update census-table-metadata

1. Makefile
    - add new line in `all` section
    - add new `clean-all` step
    - add `_merge_5_6.xls` and `_table_shells.xls` download steps
    - add `census_table_metadata.csv` step
2. census_metadata.sql
    - add table/column metadata creates
3. census_metadata_drop.sql
    - add table/column metadata drops
4. census_metadata_load.sql
    - add COPYs
5. Handle errata with table metadata:
    - Discover that the shells .xls file they include is actually an .xlsx and that python xlrd can't read the formatting info
        - Use Excel to save it as a normal .xls and replace the file
    - Discover that Census added a worksheet to the shells .xls file that is formatted completely differently
        - Use Excel to move Sheet2 before Sheet1
    - The `ACS_1yr_Seq_Table_Number_Lookup.xls` for 2014 does not reflect the changes in [the new survey](https://www.census.gov/programs-surveys/acs/technical-documentation/table-and-geography-changes/2014/1-year.html), but the [text/CSV version](http://www2.census.gov/programs-surveys/acs/summary_file/2014/documentation/user_tools/ACS_1yr_Seq_Table_Number_Lookup.txt) does so I converted it to an XLS with Excel so that the rest of my existing process would work
6. Generate the 'precomputed' metadata stuff. From census-table-metadata:
    - make
    - git add precomputed/acs2013_1yr
    - git commit
    - git push
7. Update the `unified_metadata.csv`:
    - Update the `releases_to_analyze` variable in `analyze_metadata.py` to include the new release
    - python analyze_metadata.py
    - git add
    - git commit
    - git push

#### Update census-postgres-scripts

(If you're running under embargo, you can create these files but you will have to download the files from the embargo site manually and put them in e.g. /mnt/tmp/acs2013_1yr, then unzip them)

1. make a copy of a 02_download script and modify it for the new release
2. make a copy of a 03_import script and modify it for the new release
3. commit the update
4. check it out on the EC2 instance
5. from the census-postgres-scripts dir on the EC2 instance, run:
    - ./02_download_acs_2013_3yr.sh

#### Update census-postgres

(This chunk is mostly run on a remote EC2 instance because it involves downloading the raw data dumps from Census)

1. modify meta-scripts/build_sql_files.py:
    - add a new key in config for the new release
    - commit the change to Github

- Download the `Sequence_Number_and_Table_Number_Lookup.txt` file
    - Note that the Census sometimes will only release this as an XLS. If so:
        - Open the `Sequence_Number_and_Table_Number_Lookup.xls` file in Excel and save it as a CSV
    - Copy it to /mnt/tmp/acs2013_1yr on the EC2 instance you're using to build this
    - Make sure it's named .txt, not .csv

- Make sure you've unzipped /mnt/tmp/acs2013_1yr/All_Geographies
    - (The 5yr release calls this `geog`, not `All_Geographies`)

- using census-postgres as your working dir:
    - mkdir acs2013_1yr
    - python meta-scripts/build_sql_files.py --working_dir=acs2013_1yr acs2013_1yr

- copy non-changing sql files from previous release to this one:
    - cd acs2013_1yr
    - cp ../acs2013_5yr/create_geoheader.sql \
         ../acs2013_5yr/create_tmp_geoheader.sql \
         ../acs2013_5yr/geoheader_comments.sql \
         ../acs2013_5yr/parse_tmp_geoheader.sql \
         ../acs2013_5yr/README.md \
         .

- update copied sql files to point to new release's schema
    - vi create_geoheader.sql # find/replace 2012 with 2013
    - vi create_tmp_geoheader.sql
    - vi geoheader_comments.sql
    - vi parse_tmp_geoheader.sql
    - vi README.md

- Since you probably checked out the census-postgres repo with https, you can't commit from the EC2 instance, so copy this data you just created back to your laptop:
    - scp -i ~/.ssh/censusreporter.ec2_key.pem -r \
        ubuntu@ec2-23-20-252-114.compute-1.amazonaws.com:/home/ubuntu/census-postgres/acs2013_3yr .
    - git add acs2013_3yr
    - git commit
    - git push
    - (Once you do this, go back to the EC2 instance and rm the directory you made inside of census-postgres and pull it back down from git so you have a clean repo)

#### Update census-postgres-scripts
- using census-postgres-scripts as your working dir:
    - (make sure you have a .pgpass file with your postgres database credentials in it so you don't have to type your password a dozen times in the import script)
    - ./03_import_acs_2013_1yr.sh
    - (You'll see a lot of NOTICEs flow by, but it's only important if it's an ERROR)

- Insert the table metadata (from the census-table-metadata repo)
    - Open a psql terminal: `psql -h localhost -U census census`
    - from census_metadata.sql
        - copy and execute in a psql terminal the CREATE TABLE and CREATE INDEX's for the new release
    - from census_metadata_load.sql
        - (Using a psql terminal, execute the following:. Note that a straight COPY won't work from non-superuser.)
        - \copy acs2014_1yr.census_table_metadata  FROM '/home/ubuntu/census-table-metadata/precomputed/acs2014_1yr/census_table_metadata.csv' WITH csv ENCODING 'utf8' HEADER
        - \copy acs2014_1yr.census_column_metadata FROM '/home/ubuntu/census-table-metadata/precomputed/acs2014_1yr/census_column_metadata.csv' WITH csv ENCODING 'utf8' HEADER

- Update the unified tabulation metadata (from the census-table-metadata repo)
    - Add the new release to the "releases_to_analyze" list
    - Run `python analyze_metadata.py`
    - Check in the updated analyze_metadata.py and precomputed/unified_metadata.csv

    - Truncate the existing census_tabulation_metadata on the EC2 instance:
        - truncate table census_tabulation_metadata;
    - Copy the new data into the now-empty census_tabulation_metdata table:
        - \copy census_tabulation_metadata FROM '/home/ubuntu/census-table-metadata/precomputed/unified_metadata.csv' WITH csv ENCODING 'utf8' HEADER

- Create a new database dump
    - pg_dump -h localhost -U census -n acs2014_1yr | gzip -c > acs2014_1yr_backup.sql.gz

- After embargo, remember to check in your work:
    - census-postgres/acs2013_1yr
    - census-table-metadata/precomputed/acs2013_1yr


