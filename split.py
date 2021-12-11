#! /usr/bin/python3

import hashlib
from typing import Dict, List, Union, Set, Any
import argparse
import pylibyaml
import yaml
import pickle
from colorama import Style, Fore
from segtypes.common.group import CommonSegGroup
from segtypes.segment import Segment
from segtypes.linker_entry import LinkerWriter, to_cname
from util import log
from util import options
from util import symbols
from util import palettes

VERSION = "0.7.10.1"

parser = argparse.ArgumentParser(description="Split a rom given a rom, a config, and output directory")
parser.add_argument("config", help="path to a compatible config .yaml file", nargs='+')
parser.add_argument("--target", help="path to a file to split (.z64 rom)")
parser.add_argument("--basedir", help="a directory in which to extract the rom")
parser.add_argument("--modes", nargs="+", default="all")
parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
parser.add_argument("--use-cache", action="store_true", help="Only split changed segments in config")
parser.add_argument("--visualize", action="store_true", help="Visualize the configuration file")

linker_writer: LinkerWriter
config: Dict[str, Any]

def fmt_size(size):
    if size > 1000000:
        return str(size // 1000000) + " MB"
    elif size > 1000:
        return str(size // 1000) + " KB"
    else:
        return str(size) + " B"

def initialize_segments(config_segments: Union[dict, list]) -> List[Segment]:
    seen_segment_names: Set[str] = set()
    ret = []

    for i, seg_yaml in enumerate(config_segments):
        # rompos marker
        if isinstance(seg_yaml, list) and len(seg_yaml) == 1:
            continue

        seg_type = Segment.parse_segment_type(seg_yaml)

        segment_class = Segment.get_class_for_type(seg_type)
        
        this_start = Segment.parse_segment_start(seg_yaml)
        next_start = Segment.parse_segment_start(config_segments[i + 1])

        segment: Segment = Segment.from_yaml(segment_class, seg_yaml, this_start, next_start)

        if segment.require_unique_name:
            if segment.name in seen_segment_names:
                log.error(f"segment name '{segment.name}' is not unique")

            seen_segment_names.add(segment.name)

        ret.append(segment)

    return ret

def get_segment_symbols(segment, all_segments):
    seg_syms = {}
    other_syms = {}

    for symbol in symbols.all_symbols:
        if symbols.is_symbol_isolated(symbol, all_segments) and not symbol.rom:
            if segment.contains_vram(symbol.vram_start):
                if symbol.vram_start not in seg_syms:
                    seg_syms[symbol.vram_start] = []
                seg_syms[symbol.vram_start].append(symbol)
            else:
                if symbol.vram_start not in other_syms:
                    other_syms[symbol.vram_start] = []
                other_syms[symbol.vram_start].append(symbol)
        else:
            if symbol.rom and segment.contains_rom(symbol.rom):
                if symbol.vram_start not in seg_syms:
                    seg_syms[symbol.vram_start] = []
                seg_syms[symbol.vram_start].append(symbol)
            else:
                if symbol.vram_start not in other_syms:
                    other_syms[symbol.vram_start] = []
                other_syms[symbol.vram_start].append(symbol)

    return seg_syms, other_syms

def do_statistics(seg_sizes, rom_bytes, seg_split, seg_cached):
    unk_size = seg_sizes.get("unk", 0)
    rest_size = 0
    total_size = len(rom_bytes)

    for typ in seg_sizes:
        if typ != "unk":
            rest_size += seg_sizes[typ]

    known_ratio = rest_size / total_size
    unk_ratio = unk_size / total_size

    log.write(f"Split {fmt_size(rest_size)} ({known_ratio:.2%}) in defined segments")
    for typ in seg_sizes:
        if typ != "unk":
            tmp_size = seg_sizes[typ]
            tmp_ratio = tmp_size / total_size
            log.write(f"{typ:>20}: {fmt_size(tmp_size):>8} ({tmp_ratio:.2%}) {Fore.GREEN}{seg_split[typ]} split{Style.RESET_ALL}, {Style.DIM}{seg_cached[typ]} cached")
    log.write(f"{'unknown':>20}: {fmt_size(unk_size):>8} ({unk_ratio:.2%}) from unknown bin files")

def merge_configs(main_config, additional_config):
    # Merge rules are simple
    # For each key in the dictionary
    # - If list then append to list
    # - If a dictionary then repeat merge on sub dictionary entries
    # - Else assume string or number and replace entry

    for curkey in additional_config:
        if curkey not in main_config:
            main_config[curkey] = additional_config[curkey]
        elif type(main_config[curkey]) != type(additional_config[curkey]):
            log.error(f"Type for key {curkey} in configs does not match")
        else:
            # keys exist and match, see if a list to append
            if type(main_config[curkey]) == list:
                main_config[curkey] += additional_config[curkey]
            elif type(main_config[curkey]) == dict:
                #need to merge sub areas
                main_config[curkey] = merge_configs(main_config[curkey], additional_config[curkey])
            else:
                #not a list or dictionary, must be a number or string, overwrite
                main_config[curkey] = additional_config[curkey]

    return main_config

def visualize(config_path, base_dir, target_path):
    import plotly.graph_objects as go

    global config

    # Load config
    config = {}
    for entry in config_path:
        with open(entry) as f:
            additional_config = yaml.load(f.read(), Loader=yaml.SafeLoader)
        config = merge_configs(config, additional_config)

    options.initialize(config, config_path, base_dir, target_path)

    labels = ["rom"]
    parents = [""]
    values = [0]
    types = ["rom"]

    # Initialize segments
    all_segments = initialize_segments(config["segments"])

    for segment in all_segments:
        if isinstance(segment, CommonSegGroup):
            for sub in segment.subsegments:
                if sub.size:
                    if sub.name == segment.name:
                        labels.append(sub.name + "_sub")
                    else:
                        labels.append(sub.name)
                    parents.append(segment.name)
                    types.append(sub.type)
                    values.append(sub.size)

        labels.append(segment.name)
        parents.append("rom")
        types.append(segment.type)
        values.append(segment.size)

    fig = go.Figure(go.Treemap(
        branchvalues="remainder",
        labels = labels,
        values = values,
        parents = parents,
        root_color="lightblue",
    ))

    fig.update_layout(margin = dict(t=50, l=25, r=25, b=25))

    fig.show()

def main(config_path, base_dir, target_path, modes, verbose, use_cache=True):
    global config

    log.write(f"splat {VERSION}")

    # Load config
    config = {}
    for entry in config_path:
        with open(entry) as f:
            additional_config = yaml.load(f.read(), Loader=yaml.SafeLoader)
        config = merge_configs(config, additional_config)

    options.initialize(config, config_path, base_dir, target_path)
    options.set("modes", modes)

    if verbose:
        options.set("verbose", True)

    with options.get_target_path().open("rb") as f2:
        rom_bytes = f2.read()

    if "sha1" in config:
        sha1 = hashlib.sha1(rom_bytes).hexdigest()
        e_sha1 = config["sha1"].lower()
        if e_sha1 != sha1:
            log.error(f"sha1 mismatch: expected {e_sha1}, was {sha1}")

    # Create main output dir
    options.get_base_path().mkdir(parents=True, exist_ok=True)

    processed_segments: List[Segment] = []

    seg_sizes: Dict[str, int] = {}
    seg_split: Dict[str, int] = {}
    seg_cached: Dict[str, int] = {}

    # Load cache
    if use_cache:
        try:
            with options.get_cache_path().open("rb") as f3:
                cache = pickle.load(f3)
            
            if verbose:
                log.write(f"Loaded cache ({len(cache.keys())} items)")
        except Exception:
            cache = {}
    else:
        cache = {}

    # invalidate entire cache if options change
    if use_cache and cache.get("__options__") != config.get("options"):
        if verbose:
            log.write("Options changed, invalidating cache")

        cache = {
            "__options__": config.get("options"),
        }

    # Initialize segments
    all_segments = initialize_segments(config["segments"])

    # Load and process symbols
    if options.mode_active("code"):
        log.write("Loading and processing symbols")
        symbols.initialize(all_segments)

    # Resolve raster/palette siblings
    if options.mode_active("img"):
        palettes.initialize(all_segments)

    # Scan
    log.write("Starting scan")
    for segment in all_segments:
        typ = segment.type
        if segment.type == "bin" and segment.is_name_default():
            typ = "unk"

        if typ not in seg_sizes:
            seg_sizes[typ] = 0
            seg_split[typ] = 0
            seg_cached[typ] = 0
        seg_sizes[typ] += 0 if segment.size is None else segment.size

        if segment.should_scan():
            # Check cache but don't write anything
            if use_cache:
                if segment.cache() == cache.get(segment.unique_id()):
                    continue

            if segment.needs_symbols:
                segment_symbols, other_symbols = get_segment_symbols(segment, all_segments)
                segment.given_seg_symbols = segment_symbols
                segment.given_ext_symbols = other_symbols

            segment.did_run = True
            segment.scan(rom_bytes)

            processed_segments.append(segment)

            seg_split[typ] += 1

        log.dot(status=segment.status())

    # Split
    log.write("Starting split")
    for segment in all_segments:
        if use_cache:
            cached = segment.cache()

            if cached == cache.get(segment.unique_id()):
                # Cache hit
                seg_cached[typ] += 1
                continue
            else:
                # Cache miss; split
                cache[segment.unique_id()] = cached

        if segment.should_split():
            segment.split(rom_bytes)

        log.dot(status=segment.status())

    if options.mode_active("ld"):
        global linker_writer
        linker_writer = LinkerWriter()
        for segment in all_segments:
            linker_writer.add(segment)
        linker_writer.save_linker_script()
        linker_writer.save_symbol_header()

        # write elf_sections.txt - this only lists the generated sections in the elf, not sub sections
        # that the elf combines into one section
        if options.get_create_elf_section_list_auto():
            section_list = ""
            for segment in all_segments:
                section_list += "." + to_cname(segment.name) + "\n"
            with open(options.get_elf_section_list_path(), "w", newline="\n") as f:
                f.write(section_list)

    # Write undefined_funcs_auto.txt
    if options.get_create_undefined_funcs_auto():
        to_write = [s for s in symbols.all_symbols if s.referenced and not s.defined and not s.dead and s.type == "func"]
        if len(to_write) > 0:
            with open(options.get_undefined_funcs_auto_path(), "w", newline="\n") as f:
                for symbol in to_write:
                    f.write(f"{symbol.name} = 0x{symbol.vram_start:X};\n")

    # write undefined_syms_auto.txt
    if options.get_create_undefined_syms_auto():
        to_write = [s for s in symbols.all_symbols if s.referenced and not s.defined and not s.dead and not s.type == "func"]
        if len(to_write) > 0:
            with open(options.get_undefined_syms_auto_path(), "w", newline="\n") as f:
                for symbol in to_write:
                    f.write(f"{symbol.name} = 0x{symbol.vram_start:X};\n")

    # print warnings during split
    for segment in all_segments:
        if len(segment.warnings) > 0:
            log.write(f"{Style.DIM}0x{segment.rom_start:06X}{Style.RESET_ALL} {segment.type} {Style.BRIGHT}{segment.name}{Style.RESET_ALL}:")

            for warn in segment.warnings:
                log.write("warning: " + warn, status="warn")

            log.write("") # empty line

    # Statistics
    do_statistics(seg_sizes, rom_bytes, seg_split, seg_cached)

    # Save cache
    if cache != {} and use_cache:
        if verbose:
            log.write("Writing cache")
        with open(options.get_cache_path(), "wb") as f4:
            pickle.dump(cache, f4)

if __name__ == "__main__":
    args = parser.parse_args()
    if args.visualize:
        visualize(args.config, args.basedir, args.target)
    else:
        main(args.config, args.basedir, args.target, args.modes, args.verbose, args.use_cache)
