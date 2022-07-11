from typing import List, Callable, Any, Optional, Tuple, Dict, Set
import os
import re
import sqlite3
import sys
import time
import json

from .producer import GenericProducer
from .creator import Creator
from pylib.unique_heap import UniqueHeap
from pylib.terminal_color import fg_gray


GenericCreator = Creator[Any, Any]

################################################################################
# Scheduler is a tool for scheduling jobs to be completed based on the
# existence and modification of files.
################################################################################

ProducerIndexType = int

# Tuple[ProducerId, StringifiedOrHashedGroups]
# Keeping the producerindex first in this tuple is important for sorting reasons
CreatorIndexType = Tuple[ProducerIndexType, str]




def get_hashable_matchgroups(groups: Dict[str, str]) -> str:
    return json.dumps(groups, sort_keys=True)
################################################################################
# A controller and watcher for the set of producers and creators
################################################################################
class Scheduler:
    # A list of producers that can be referenced by id
    producer_list: List[GenericProducer]

    # # A list of creators that can be referenced by id
    # creator_list: Dict[int, GenericCreator]
    # last_creator_list_index: int


    creator_list: Dict[CreatorIndexType, GenericCreator]


    # # A map of a creator index to a producer index that spawned the creator
    # # TODO: This should probably be a part of creator_list instead of a
    # #       seperate object sitting around.
    # creator_producer: Dict[int, int]

    # A map of output files to the creator indexes that create them
    output_file_maps: Dict[str, CreatorIndexType]

    # A map of input files to the creator index that consume them
    input_file_maps: Dict[str, Set[CreatorIndexType]]

    filecache: sqlite3.Connection

    verbose: bool = False

    ############################################################################
    #
    ############################################################################
    def __init__(
        self,
        # producers: List[GenericProducer],
        producer_list: List[GenericProducer],
        # filepaths: List[str] = []
        initial_filepaths: List[str] = []
    ):
        self.producer_list = producer_list
        self.creator_list = {}
        # self.last_creator_list_index = -1
        # self.creator_producer = {}
        self.output_file_maps = {}
        self.input_file_maps = {}

        self.filecache = self.init_producer_cache(self.producer_list)

        self.add_or_update_files(initial_filepaths)

    ############################################################################
    # add_or_update_files
    #
    # This function should be called whenever a file is added to the source
    # tree, or updated inside the source tree. It should be called with a list
    # of all files on program initialization.
    ############################################################################
    def add_or_update_files(self, files: List[str]) -> None:
        self.build_new_creators(files)
        self.process_files(files)


    ############################################################################
    # delete_creators_with_input_files
    #
    #
    ############################################################################
    def delete_creators_with_input_files(self, files: List[str]) -> None:
        creator_indexes_to_delete: Set[CreatorIndexType] = set()

        for file in files:
            if file in self.input_file_maps:
                for creator_index in self.input_file_maps[file]:
                    creator_indexes_to_delete.add(creator_index)

        # Get all of the output files from the creator and then delete them
        # from the output cache. Each output file can only be generated by one
        # creator at a time so we know that these output files are only linked
        # to this creator. We do this instead of looping through all elements
        # in self.output_file_map so that we wont slow down this function as
        # more files are added to the list.
        for creator_index in creator_indexes_to_delete:
            self.delete_creator(creator_index)

    def delete_creator(self, creator_index: CreatorIndexType) -> None:
        creator = self.creator_list[creator_index]
        for output_file in creator.flat_output_paths():

            # Sanity check that the file is indeed a part of the creator we
            # will be deleting.
            output_file_creator_index =self.output_file_maps[output_file]
            if output_file_creator_index != creator_index:
                raise ValueError("Trying to delete an output file index for a creator which is not being deleted")

            del self.output_file_maps[output_file]

        # Delete any input file cache reference to this creator
        for input_file in creator.flat_input_paths():
            self.input_file_maps[input_file].remove(creator_index)

            # If this was the last creator this file referenced then delete
            # the entire element to keep it clean.
            if len(self.input_file_maps[input_file]) == 0:
                del self.input_file_maps[input_file]


        # Delete the creator itself
        del self.creator_list[creator_index]




    ############################################################################
    # build_new_creators
    #
    # Parse a series of files into all of the active producers and get a list
    # of the new creators that those files would create. Return the creator
    # indexes of the creators that are created from them.
    ############################################################################
    def build_new_creators(self, files: List[str]) -> List[Tuple[ProducerIndexType, CreatorIndexType]]:
        # Clean any creators that have any of these files as inputs
        self.delete_creators_with_input_files(files)

        # Insert or update all files in the database
        for producer_index, producer in enumerate(self.producer_list):
            for path in files:
                for field_name, pattern in producer.regex_field_patterns().items():
                    match: Optional[re.Match[str]] = re.match(pattern, path)

                    if match is None:
                        continue

                    # Delete the file from the database if it exists
                    self.remove_file_from_database(self.filecache, producer_index, field_name, path)

                    # Insert a file into the database. If it already exists then
                    # it is updated to be marked as a fresh file.
                    self.insert_new_file(self.filecache, producer_index, field_name, path, match.groupdict())



        new_creators: List[Tuple[ProducerIndexType, CreatorIndexType]] = []
        # Build a list of creators
        for producer_index, producer in enumerate(self.producer_list):
            input_datas = self.query_filesets(self.filecache, producer_index)
            for input_data in input_datas:
                input_file, input_groups = input_data

                new_input_data, output_data = producer.paths(input_file, input_groups)

                categories = producer.categories

                if callable(categories):
                    categories = categories(new_input_data, output_data)

                creator = Creator(
                    input_paths=new_input_data,
                    output_paths=output_data,
                    function=producer.function,
                    categories=categories
                )


                new_creator_index: CreatorIndexType = (producer_index, get_hashable_matchgroups(input_groups))

                # Check if an old creator with all the same match-groups exists.
                # The only time this will happen is if a creator is being
                # remade. Then delete the old creator and any input/output
                # caches it had.
                if new_creator_index in self.creator_list:
                    self.delete_creator(new_creator_index)

                # Detect duplicate creators and error if any exist
                for file in creator.flat_output_paths():
                    if file in self.output_file_maps:
                        raise ValueError("Two Creators with the same output file exist. Was a creator not destroyed properly before being remade?\n\tExisting:{existing_creator}\n\tNew:{new_creator}".format(
                            existing_creator=self.creator_list[self.output_file_maps[file]],
                            new_creator=creator
                        ))

                # self.last_creator_list_index += 1
                self.creator_list[new_creator_index] = creator
                # self.creator_producer[self.last_creator_list_index] = producer_index
                new_creators.append((producer_index, new_creator_index))


                for file in creator.flat_input_paths():
                    if file not in self.input_file_maps:
                        self.input_file_maps[file] = set()

                    self.input_file_maps[file].add(new_creator_index)

                for file in creator.flat_output_paths():
                    self.output_file_maps[file] = new_creator_index

        self.mark_all_files_old(self.filecache)

        return new_creators

    ############################################################################
    # process_files
    #
    # Process a list of files through all of the currently active creators.
    ############################################################################
    def process_files(self, files: List[str]) -> None:
        # Heap[Tuple[ProducerIndex, CreatorIndex]]
        creators_to_update: UniqueHeap[CreatorIndexType] = UniqueHeap()

        # Fill the creators_to_update will all the producer/creator pairs
        for file in files:
            # If the file is not used in any creator, ignore it
            if file not in self.input_file_maps:
                continue

            creator_indexes: Set[CreatorIndexType] = self.input_file_maps[file]
            for creator_index in creator_indexes:
                creators_to_update.push(creator_index)

        # Process each creator until there are none left
        while len(creators_to_update) > 0:
            creator_index: CreatorIndexType = creators_to_update.pop()
            producer_index: ProducerIndexType = creator_index[0]

            creator: GenericCreator = self.creator_list[creator_index]

            output_files: List[str] = creator.flat_output_paths()
            input_files: List[str] = creator.flat_input_paths()

            if all_files_exist(creator.flat_output_paths()):
                # If all of the output files are newer then all of the input files
                # then do not regenerate this producer.
                oldest_output = get_oldest_modified_time(output_files)
                newest_input = get_newest_modified_time(input_files)
                # "newer" is a larger number
                if oldest_output > newest_input:
                    continue

            # Build creators for any of the files generated by this creator
            # They will be picked up in the next step where we add them to the
            # creators_to_update variable.
            self.build_new_creators(output_files)

            # Add the output files to the prioritized list of things to process.
            # These will be automatically de-duplicated if they are already present.
            # self.make_creators(output_files)
            for file in output_files:
                # If the file is not used in any creator, ignore it
                if file not in self.input_file_maps:
                    continue

                creator_indexes: Set[CreatorIndexType] = self.input_file_maps[file]
                for creator_index in creator_indexes:
                    creators_to_update.push(creator_index)

            # Pre-create any directories so the functions can always assume that
            # the directories exist and just focus on creating the files.
            build_required_directories(output_files)

            print()
            print(creator.categories)


            if len(input_files) > 5:
                print(fg_gray("  " + input_files[0]))
                print(fg_gray("  " + input_files[1]))
                print(fg_gray("  " + input_files[2]))
                print(fg_gray("  " + input_files[3]))
                print(fg_gray("  ...and {} other files".format(len(input_files)-4)))


            else:
                for i, file in enumerate(input_files):
                    print(fg_gray("  " + file))

            print(fg_gray("  │"))

            for i, file in enumerate(output_files):

                pipe_character = "├"
                if (i == len(output_files)-1):
                    pipe_character = "└"
                    # pipe_character = "╰"

                print(fg_gray("  {pipe_character}── {file}".format(
                    pipe_character=pipe_character,
                    file=file,
                )))


            start = time.time()
            creator.run()
            duration = time.time() - start
            print(fg_gray("  Completed in {:.2f}s".format(duration)))

    ############################################################################
    # all_paths_in_dir
    #
    # A helper function to use for initial_filepaths when you want to add all
    # of the files under a particular directory.
    ############################################################################
    @staticmethod
    def all_paths_in_dir(base_dir: str, ignore_paths: List[str]) -> List[str]:
        paths: List[str] = []

        for root, dirs, files in os.walk(base_dir):
            # Strip the "current directory" prefix because that makes it more
            # annoying to match things on.
            if root.startswith("./"):
                root = root[2:]

            # Add all of the files and directories unless the path matches an ignore path
            for path in dirs + files:
                full_path = os.path.join(root, path)

                skip = False
                for ignore_path in ignore_paths:
                    if full_path.startswith(ignore_path):
                        skip = True
                        break
                if skip:
                    continue

                paths.append(full_path)

        return paths


    def delete_files(self, files: List[str]) -> None:
        self.delete_creators_with_input_files(files)
        # self.remove_files_from_database(files)
        # self.remove_file_from_database

        # Delete all files to delete in the database
        for producer_index, producer in enumerate(self.producer_list):
            for path in files:
                for field_name, pattern in producer.regex_field_patterns().items():
                    match: Optional[re.Match[str]] = re.match(pattern, path)

                    if match is None:
                        continue

                    self.remove_file_from_database(self.filecache, producer_index, field_name, path)


        pass
    ############################################################################
    ############################################################################
    # SQL LOGIC
    ############################################################################
    ############################################################################


    ############################################################################
    # get_field_table_name
    #
    # A helper function to produce the name of the table that stores matches
    # for a particular field.
    # TODO: The SQL logic should somehow be moved to scheduler.py
    ############################################################################
    @staticmethod
    def get_field_table_name(producer_index: int, field_id: str) -> str:
        return "producer{producer_index}_field{field_id}_matches".format(
            producer_index=producer_index,
            field_id=field_id
        )

    @staticmethod
    def get_match_group_column_name(producer: GenericProducer, group_name: str) -> str:
        return "group_{group_id}".format(
            group_id=producer.get_match_group_id(group_name)
        )

    ############################################################################
    # init_producer_cache
    #
    # Create the cache database for storing all the files that match a producer
    # field, and then initialize all of the tables in the database.
    # TODO: Logic from producers using sql commands should be moved to this
    # file instead.
    ############################################################################
    def init_producer_cache(self, producer_list: List[GenericProducer]) -> sqlite3.Connection:
        db = sqlite3.connect(':memory:')

        for producer_index, producer in enumerate(producer_list):
            for init_query in self.init_table_query(producer_index):
                with db:
                    db.execute(init_query)

        return db

    ############################################################################
    # init_table_query
    #
    # Create a series of sql query strings that are used to create all of the
    # tables for each field in this producer.
    ############################################################################
    def init_table_query(self, producer_index: int) -> List[str]:
        producer: GenericProducer = self.producer_list[producer_index]

        query_strings: List[str] = []

        for field_name in producer.regex_field_patterns():

            field_id: str = producer.get_field_id(field_name)

            field_table_name = Scheduler.get_field_table_name(
                producer_index=producer_index,
                field_id=field_id
            )

            table_columns: List[str] = [
                "filename TEXT UNIQUE",
                "is_updated INTEGER",
            ]

            for group_name in producer.get_match_groups(field_name):

                table_columns.append(Scheduler.get_match_group_column_name(
                    producer=producer,
                    group_name=group_name,
                )+" TEXT")

            query_string = "CREATE TABLE {field_table_name} ({table_columns});".format(
                field_table_name=field_table_name,
                table_columns=", ".join(table_columns)
            )

            query_strings.append(query_string)

        return query_strings

    ############################################################################
    # insert
    #
    # Insert a file that has matched a field for this producer into the
    # database table for that field.
    ############################################################################
    def insert_new_file(
        self,
        db: sqlite3.Connection,
        producer_index: int,
        field_name: str,
        filename: str,
        groups: Dict[str, str]
    ) -> None:
        
        query_string, binds = self.insert_new_file_querystring(
            producer_index=producer_index,
            field_name=field_name,
            filename=filename,
            groups=groups
        )

        with db:
            db.execute(
                query_string,
                binds,
            )

    def insert_new_file_querystring(
        self,
        producer_index: int,
        field_name: str,
        filename: str,
        groups: Dict[str, str]
    ) -> Tuple[str, List[str]]:
        producer = self.producer_list[producer_index]
        field_id = producer.get_field_id(field_name)
        table_name = Scheduler.get_field_table_name(producer_index=producer_index, field_id=field_id)

        fields = ["filename", "is_updated"] + [Scheduler.get_match_group_column_name(producer=producer, group_name=group_name) for group_name in groups.keys()]

        binds = [filename, 1] + list(groups.values())

        # query_string: str = "INSERT INTO {table} ({fields}) VALUES ({value_binds}) ON CONFLICT(filename) DO UPDATE SET is_updated=1".format(
        query_string: str = "INSERT INTO {table} ({fields}) VALUES ({value_binds})".format(
            table=table_name,
            fields=", ".join(fields),
            value_binds=", ".join("?" * len(fields))
        )

        return query_string, binds

    def remove_file_from_database(
        self,
        db: sqlite3.Connection,
        producer_index: int,
        field_name: str,
        filename: str,
    ) -> None:

        query_string = self.remove_file_from_database_sql(producer_index, field_name)
        with db:
            db.execute(
                query_string,
                {
                    "filename": filename
                },
            )

    def remove_file_from_database_sql(
        self,
        producer_index: int,
        field_name: str,
    ) -> str:

        producer = self.producer_list[producer_index]
        table_name = Scheduler.get_field_table_name(
            producer_index=producer_index,
            field_id= producer.get_field_id(field_name)
        )
        query_string: str = "DELETE FROM {table} WHERE filename = :filename".format(
            table=table_name
        )

        return query_string


    ############################################################################
    # query_filesets
    #
    # Query all of the valid combinations of files that can be used for this
    # producer using the field matches that have been stored in the database.
    ############################################################################
    # def query_filesets(self, db: sqlite3.Connection, producer_index: int) -> List[Tuple[InputFileDatatype, Dict[str, str]]]:
    def query_filesets(self, db: sqlite3.Connection, producer_index: int) -> List[Tuple[Any, Dict[str, str]]]:
        query_string = self.new_filesets_querystring(producer_index)

        #print(query_string)

        producer = self.producer_list[producer_index]

        # output_data: List[Tuple[InputFileDatatype, Dict[str, str]]] = []
        output_data: List[Tuple[Any, Dict[str, str]]] = []
        with db:
            cur = db.execute(
                query_string,
            )

            columns = [ x[0] for x in cur.description ]
            columns_lookup = { value: index for index, value in enumerate(columns) }
            # print(columns_lookup)

            for row in cur.fetchall():

                new_element = {}
                groups: Dict[str, str] = {}

                for new_element_field_name, pattern in producer.input_path_patterns_dict().items():
                    new_element_field_id = producer.get_field_id(new_element_field_name)
                    if pattern == "":
                        new_element[new_element_field_name] = ""
                        continue
                    elif pattern == []:
                        new_element[new_element_field_name] = []
                        continue

                    value: str = row[columns_lookup["field_"+new_element_field_id]]
                    if isinstance(pattern, str):
                        new_element[new_element_field_name] = value
                    elif isinstance(pattern, list):
                        new_element[new_element_field_name] = sorted(parse_comma_escape(value))
                    else:
                        raise TypeError()

                for group_name in producer.get_all_match_groups():
                    group_id = producer.get_match_group_id(group_name)
                    groups[group_name] = row[columns_lookup["group_"+group_id]]


                # If at least one file is updated then this creator should be
                # constructed.
                is_updated = row[columns_lookup["is_updated"]]
                if is_updated > 0:
                    output_data.append((new_element, groups))

        return output_data


    def new_filesets_querystring(self, producer_index: int) -> str:

        producer = self.producer_list[producer_index]


        # A list of columns to select. Should end up as the union between
        # every field and every match group
        columns: List[str] = []

        # A list of tables to select FROM. These should corrispond exactly to
        # every non-empty field in the InputFieldDatatype.
        tables: List[str] = []

        # A list of columns that will be GROUP BY'ed in order to merge list
        # fields into a single row so they can be accurately inserted into
        # the InputFileDatatype.
        group_by_columns: List[str] = []

        # A map of each group to the list of tables that group is in
        field_groups: Dict[str, List[str]] = {}


        field_wheres: List[str] = []


        update_tracking_columns: List[str] = []

        # mypy complains about iterating over a typeddict even though it is a dict
        for field_name, field in producer.input_path_patterns_dict().items():
            if field == "":
                continue
            elif field == []:
                continue

            field_id = producer.get_field_id(field_name)

            table_name = Scheduler.get_field_table_name(
                producer_index=producer_index,
                field_id=field_id
            )

            field_alias="\"field_{field_id}\"".format(
                field_id=field_id,
            )

            if isinstance(field, str):
                columns.append("{table_name}.filename AS {field_alias}".format(
                    table_name=table_name,
                    field_alias=field_alias,
                ))
                group_by_columns.append(str(len(columns)))


            # If the field is a list then we want to grab each file and put it into a list
            # this is done by using
            elif isinstance(field, list):
                columns.append("GROUP_CONCAT(REPLACE(REPLACE({table_name}.filename, '\\','\\\\'), ',', '\\,'), ',') AS {field_alias}".format(
                    table_name=table_name,
                    field_alias=field_alias,
                ))
            else:
                raise TypeError("Expected either a str or a list")

            tables.append(table_name)

            for match_group_name in producer.get_match_groups(field_name):
                if match_group_name not in field_groups:
                    field_groups[match_group_name] = []

                field_groups[match_group_name].append(table_name)

            # Add the is_updated column from this table to the list of columns
            # to sum as a check if any of the files inside are updated.
            update_tracking_columns.append("{table_name}.is_updated".format(
                table_name=table_name
            ))


        field_joins: List[str] = []
        for match_group_name, group_tables in field_groups.items():
            first_table = group_tables[0]

            columns.append("{first_table}.{group_column_name}".format(
                first_table=first_table,
                group_column_name=Scheduler.get_match_group_column_name(producer, match_group_name)
            ))
            group_by_columns.append(str(len(columns)))

            for table in group_tables[1:]:
                field_joins.append("{first_table}.{group_column_name} = {table}.{group_column_name}".format(
                    first_table=first_table,
                    group_column_name=Scheduler.get_match_group_column_name(producer, match_group_name),
                    table=table,
                ))

        # Prevent the WHERE clause from being blank ever.
        # TODO: The WERE clause should probably just be removed entirely but the
        # query is already imperfect by not using JOINs instead so this will be
        # left until we re-evaluate the query again.
        if len(field_joins) == 0:
            field_joins = ["1=1"]


        columns.append(
            "SUM({}) AS \"is_updated\"".format(
                "+".join(update_tracking_columns)
            )
        )

        query_string = "SELECT {columns} FROM {field_tables} WHERE {field_wheres} GROUP BY {group_by_columns};".format(
            columns=", ".join(columns),
            field_tables=", ".join(tables),
            field_wheres=" AND ".join(field_wheres + field_joins),
            group_by_columns=", ".join(group_by_columns)
        )

        return query_string


    def mark_all_files_old(self, db: sqlite3.Connection) -> None:
        for mark_files_query in self.mark_all_files_old_querystrings():
            with db:
                db.execute(mark_files_query)

    def mark_all_files_old_querystrings(self) -> List[str]:

        query_strings = []
        for producer_index, producer in enumerate(self.producer_list):
            for field_name, field in producer.input_path_patterns_dict().items():
                if field == "":
                    continue
                elif field == []:
                    continue

                field_id = producer.get_field_id(field_name)

                table_name = Scheduler.get_field_table_name(
                    producer_index=producer_index,
                    field_id=field_id
                )

                query_strings.append("UPDATE {table_name} SET is_updated = 0 WHERE is_updated != 0;".format(
                    table_name=table_name
                ))

        return query_strings



################################################################################
# all_files_exist
#
# A helper function that will check if every file in a list exist. If one or
# more files does not exist then it will return False. If an empty list is
# passed in then it will return True.
################################################################################
def all_files_exist(files: List[str]) -> bool:
    for file in files:
        if not os.path.exists(file):
            return False
    return True


################################################################################
# build_required_directories
#
# Takes in a list of files and then creates all of the directories needed in
# order for those files to be written to if they do not already exist.
################################################################################
def build_required_directories(files: List[str]) -> None:
    for file in files:
        directory = os.path.dirname(file)
        if not os.path.exists(directory):
            os.makedirs(directory)


################################################################################
# get_newest_modified_time
#
# This function takes in a list of files and returns the most recent time any
# of them were modified.
################################################################################
def get_newest_modified_time(paths: List[str]) -> float:
    return get_aggregated_modified_time(
        paths=paths,
        aggregator=max,
        default=sys.float_info.max,
    )


################################################################################
# get_oldest_modified_time
#
# This function takes in a list of files and returns the least recent time any
# of them were modified.
################################################################################
def get_oldest_modified_time(paths: List[str]) -> float:
    return get_aggregated_modified_time(
        paths=paths,
        aggregator=min,
        default=0,
    )


################################################################################
# get_aggregated_modified_time
#
# A helper function for get_newest_modified_time() and get_oldest_modified_time()
# which use almost identical logic save for the default values of non existent
# files, and the aggregator function used over all of the file ages.
################################################################################
def get_aggregated_modified_time(
    paths: List[str],
    aggregator: Callable[[List[float]], float],
    default: float
) -> float:
    # Duplicate the paths list so we can modify it. This allows us to avoid
    # recursion by just appending the values.
    paths = list(paths)
    time_list: List[float] = []
    for path in paths:
        # If a path is missing add the default value instead
        if not os.path.exists(path):
            time_list.append(default)
            continue

        # If a path is a directory add all its children to the paths list
        if (os.path.isdir(path)):
            for subpath in os.listdir(path):
                paths.append(os.path.join(path, subpath))
        else:
            time_list.append(os.path.getmtime(path))

    # Sanity check that there are timestamps in the list before passing them
    # to the aggregator.
    if len(time_list) == 0:
        return default

    return aggregator(time_list)


################################################################################
# parse_comma_escape
#
# Parses the escaped comma string returned from the SQL query back into an
# array. The query escapes all backslashes and commas, then uses a comma to
# delimite each element in the array.
# TODO: The SQL logic should somehow be moved to scheduler.py
################################################################################
def parse_comma_escape(input_string: str) -> List[str]:
    output_strings: List[str] = [""]
    last_character: str = ""
    for character in input_string:
        if character == "," and last_character != "\\":
            output_strings.append("")
        elif character == "\\" and last_character != "\\":
            pass
        else:
            output_strings[-1] += character

        last_character = character

    return output_strings