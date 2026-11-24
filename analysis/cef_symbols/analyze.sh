#!/bin/bash
cd "$(dirname "$0")"  # dir with libcef.so.orig (unstripped official CEF build)
{
echo "== gdb at 0x77270aa"
gdb -batch -nx -ex "info symbol 0x77270aa" -ex "info line *0x77270aa" -ex "x/3i 0x77270aa" -ex "x/2xb 0x77270aa" libcef.so.orig 2>&1 | tail -7
echo "== disasm OnMemoryDump (0x7726ee0..0x7727100)"
objdump -d --no-show-raw-insn --start-address=0x7726ee0 --stop-address=0x7727100 libcef.so.orig | grep -E "^ +[0-9a-f]+:" > disasm.txt
grep -n "77270aa" disasm.txt
echo "== callers jumping to 77270aa"
for a in $(grep -E "j[a-z]+ +77270aa" disasm.txt | awk -F: "{print \$1}" | tr -d " "); do
  echo "-- 0x$a"; grep -B4 "^ *$a:" disasm.txt
  addr2line -f -C -i -e libcef.so.orig 0x$a
done
echo DONE
} > analyze.out 2>&1
