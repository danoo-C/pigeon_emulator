PigeonOS 0.1

The installer copied this from
the disc with everything else,
and the hard disk now boots
/boot.bin: the kernel, which
runs what /etc/boot.conf
names: a splash screen, then
the shell.

Try, at the prompt:
  ls -l /bin
  mkdir /notes
  cp /docs/readme.txt /notes
  cat /notes/readme.txt
  more ls -l /bin
  edit /notes/readme.txt
  graph     (Esc comes back)
  cube      (Esc comes back)
  files     (Esc comes back)
  img /etc/bmp/pigeon.bmp
            (Esc comes back)
  pgs /docs/hello.pgs
            a script; read it
            with more or edit
  explorer  the file explorer:
            click about, Esc
            comes back. What it
            opens files with is
            /etc/explorer.conf
  help

The prompt is the file
/etc/shell_header.conf.
