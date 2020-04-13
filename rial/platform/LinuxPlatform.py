from shutil import which
from typing import List

from rial.linking.linking_options import LinkingOptions
from rial.platform.IPlatform import IPlatform


class LinuxPlatform(IPlatform):
    def get_source_file_extension(self) -> str:
        return super().get_source_file_extension()

    def get_object_file_extension(self) -> str:
        return super().get_object_file_extension()

    def get_asm_file_extension(self) -> str:
        return super().get_asm_file_extension()

    def get_ir_file_extension(self) -> str:
        return super().get_ir_file_extension()

    def get_bc_file_extension(self) -> str:
        return super().get_bc_file_extension()

    def get_exe_file_extension(self) -> str:
        return ""

    def get_link_options(self) -> LinkingOptions:
        opts = LinkingOptions()
        opts.linker_executable = which("cc")

        # We want to be able to strip as much executable code as possible
        # from the linker command line, and this flag indicates to the
        # linker that it can avoid linking in dynamic libraries that don't
        # actually satisfy any symbols up to that point (as with many other
        # resolutions the linker does). This option only applies to all
        # following libraries so we're sure to pass it as one of the first
        # arguments.
        opts.linker_pre_args.append("-Wl,--as-needed")

        # Always enable NX protection when it is available
        opts.linker_pre_args.append("-Wl,-z,noexecstack")

        # Use 64-bit
        opts.linker_pre_args.append("-m64")

        return opts