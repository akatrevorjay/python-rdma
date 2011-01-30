#!/usr/bin/python
# ./mkstructs.py -x iba_transport.xml -x iba_12.xml -x iba_13_4.xml -x iba_13_6.xml -x iba_14.xml -x iba_15.xml -x iba_16_1.xml -x iba_16_3.xml -x iba_16_4.xml  -o ../rdma/IBA_struct.py -t ../tests/iba_struct.py
'''This script converts the XML descriptions of IB structures into python
   classes and associated codegen'''
from __future__ import with_statement;
import sys,optparse,re,os;
from xml.etree import ElementTree;
from contextlib import contextmanager;

# From IBA.py - including this by name creates a circular module dependency
# that is easier to break this way.
MAD_METHOD_GET = 0x01;
MAD_METHOD_SET = 0x02;
MAD_METHOD_SEND = 0x03;
MAD_METHOD_GET_RESP = 0x81;
MAD_METHOD_TRAP = 0x05;
MAD_METHOD_TRAP_REPRESS = 0x07;
MAD_METHOD_GET_TABLE = 0x12;
MAD_METHOD_GET_TRACE_TABLE = 0x13;
MAD_METHOD_GET_MULTI = 0x14;
MAD_METHOD_DELETE = 0x15;
MAD_METHOD_RESPONSE = 0x80;

methodMap = {};
prefix = ("Subn","CommMgt","Performance","BM","DevMgt","SubnAdm","SNMP");
for I in prefix:
    for J in ("Get","Set","Send","Trap","Delete"):
        methodMap[I+J] = "MAD_METHOD_%s"%(J.upper());
    methodMap[I+"GetTable"] = "MAD_METHOD_GET_TABLE";
    methodMap[I+"GetTraceTable"] = "MAD_METHOD_GET_TRACE_TABLE";
    methodMap[I+"GetMulti"] = "MAD_METHOD_GET_MULTI";

@contextmanager
def safeUpdateCtx(path):
    """Open a temporary file path.tmp, return it, then close it and rename it
    to path using safeUpdate as a context manager"""
    tmp = path + ".tmp";
    try:
        os.unlink(tmp);
    except: pass;

    f = file(tmp,"wt");
    yield f;
    f.close();
    os.rename(tmp,path);

class Type(object):
    """Hold a single typed field in the structure"""
    def __init__(self,xml):
        self.count = int(xml.get("count","1"));
        self.bits = int(xml.get("bits"));
        self.off = xml.get("off");
        g = re.match("^(\d+)\[(\d+)\]$",self.off);
        if g:
            g = g.groups();
            self.off = int(g[0]) + int(g[1])*8;
        else:
            self.off = int(self.off)*8;
        self.type = xml.get("type");

    def lenBits(self):
        return self.bits*self.count;
    def isObject(self):
        return self.type and self.type[:7] == 'struct ';
    def initStr(self):
        base = "0";
        if self.isObject():
            base = self.type[7:] + "()";
        elif self.bits > 64:
            base = "bytearray(%u)"%(self.bits/8);
        if self.count != 1:
            if self.bits <= 8:
                return "bytearray(%u)"%(self.count);
            return "[%s]*%u"%(base,self.count);
        return base;
    def isAligned(self):
        if self.bits >= 32:
            return self.bits % 32 == 0 and self.off % 32 == 0;
        return (self.bits == 8 or self.bits == 16) and \
               (self.off % self.bits) == 0;
    def getStruct(self):
        if self.isObject():
            return self.type[7:];
        return None;

class Struct(object):
    '''Holds the a single structure'''
    def __init__(self,xml):
        self.name = xml.get("name");
        self.size = int(xml.get("bytes"));
        self.desc = "%s (section %s)"%(xml.get("desc"),xml.get("sect"));

        self.mgmtClass = xml.get("mgmtClass");
        self.mgmtClassVersion = xml.get("mgmtClassVersion");
        self.methods = xml.get("methods");
        self.attributeID = xml.get("attributeID");

        self.mb = [];
        self.packCount = 0;
        self.reserved = 0;
        def toReserved(s):
            if s is None:
                self.reserved = self.reserved + 1;
                return "reserved%u"%(self.reserved);
            return s;

        for I in xml.getiterator("mb"):
            self.mb.append((toReserved(I.text),Type(I)));
        assert(sum((I[1].lenBits() for I in self.mb),0) <= self.size*8);

        self.mbGroup = self.groupMB();

    def groupMB(self):
        """Take the member list and group it into struct format characters. We
        try to have 1 format character for each member, but if that doesn't
        work out we group things that have to fit into a 8, 16 or 32 bit
        word."""

        groups = [];
        curGroup = [];
        off = 0;
        for I in self.mb:
            bits = I[1].lenBits();
            curGroup.append(I);

            if (off == 0 and (off + bits) % 32 == 0) or \
               (off + bits) % 32 == 0:
                if reduce(lambda a,b:a and b[1].isAligned(),curGroup,True):
                    for J in curGroup:
                        groups.append((J,));
                else:
                    groups.append(curGroup);
                curGroup = [];
                off = 0;
                continue;
            off = off + bits;
        assert(not curGroup);
        return groups;

    def bitsToFormat(self,bits):
        if bits == 8:
            return "B";
        if bits == 16:
            return "H";
        if bits == 32:
            return "L";
        if bits == 64:
            return "Q";
        assert(False);

    def formatSinglePack(self,bits,name,mbt):
        other = mbt.getStruct();
        if other:
            if mbt.count == 1:
                return (None,("%s.pack_into(buffer,offset + %u)"%(name,mbt.off/8),
                              "%s.unpack_from(buffer,offset + %u)"%(name,mbt.off/8)),mbt.lenBits());

            lst = [];
            for I in range(0,mbt.count):
                lst.append((None,("%s[%u].pack_into(buffer,offset + %u)"%(name,I,I*mbt.bits/8 + mbt.off/8),
                                  "%s[%u].unpack_from(buffer,offset + %u)"%(name,I,I*mbt.bits/8 + mbt.off/8)),
                            mbt.lenBits()));
            return lst;
        if mbt.type == "HdrIPv6Addr":
            return ("[:16]",name,bits);
        if mbt.count == 1:
            if mbt.type is None and bits > 64:
                return ("[:%u]"%(bits/8),name,bits);
            return (self.bitsToFormat(bits),name,bits);
        if mbt.bits == 8:
            return ("[:%u]"%(bits/8),name,bits);
        if mbt.bits == 16 or mbt.bits == 32:
            res = []
            for I in range(0,mbt.count):
                res.append((self.bitsToFormat(mbt.bits),"%s[%u]"%(name,I),
                           mbt.bits));
            return res;

        # Must be a bit array
        assert(bits % 8 == 0)
        return (None,("rdma.binstruct.packArray8(buffer,%u,%u,%u,%s)"%\
                      (mbt.off/8,mbt.bits,mbt.count,name),
                      "rdma.binstruct.unpackArray8(buffer,%u,%u,%u,%s)"%\
                      (mbt.off/8,mbt.bits,mbt.count,name)),
                bits);

    def structFormat(self,groups,prefix):
        res = [];
        for I in groups:
            bits = sum(J[1].lenBits() for J in I);
            assert(bits == 8 or bits == 16 or bits == 32 or bits % 32 == 0);
            if len(I) == 1:
                x = self.formatSinglePack(bits,prefix + I[0][0],I[0][1]);
                if isinstance(x,list):
                    res.extend(x);
                else:
                    res.append(x);
                continue;

            func = "_pack_%u_%u"%(self.packCount,bits);
            self.packCount = self.packCount + 1;

            pack = ["@property","def %s(self):"%(func)];
            unpack = ["@%s.setter"%(func),"def %s(self,value):"%(func)];
            tmp = [];
            off = bits;
            for J in I:
                off = off - J[1].bits;
                tmp.append("((%s%s & 0x%X) << %u)"%(prefix,J[0],(1 << J[1].bits)-1,off));
                unpack.append("    %s%s = (value >> %u) & 0x%X;"%(prefix,J[0],off,(1 << J[1].bits)-1));
            pack.append("    return %s"%(" | ".join(tmp)));
            self.funcs.append(pack);
            self.funcs.append(unpack);

            res.append((self.bitsToFormat(bits),"self.%s"%(func),bits));
        return res;

    def genFormats(self,fmts,pack,unpack):
        """Split into struct processing blocks and byte array assignment
        blocks"""
        off = 0;
        sfmts = [[]];
        sfmtsOff = [];
        fmtsOff = 0;
        for I in fmts:
            if I[0] is None:
                pack.append("    %s;"%(I[1][0]));
                unpack.append("    %s;"%(I[1][1]));
                off = off + I[2];
                continue;
            if I[0][0] == '[':
                assert off % 8 == 0 and I[2] % 8 == 0;
                pack.append("    buffer[offset + %u:offset + %u] = %s"%\
                            (off/8,off/8 + I[2]/8,I[1]));
                unpack.append("    %s = buffer[offset + %u:offset + %u]"%\
                              (I[1],off/8,off/8 + I[2]/8));
                off = off + I[2];
                continue;
            if fmtsOff != off and sfmts[-1]:
                sfmts.append([])

            if not sfmts[-1]:
                sfmtsOff.append(off);
            sfmts[-1].append(I);
            off = off + I[2];
            fmtsOff = off;

        for I,off in zip(sfmts,sfmtsOff):
            pack.append("    struct.pack_into('>%s',buffer,offset+%u,%s);"%\
                     ("".join(J[0] for J in I),
                      off/8,
                      ",".join(J[1] for J in I)));
            unpack.append("    (%s,) = struct.unpack_from('>%s',buffer,offset+%u);"%\
                          (",".join(J[1] for J in I),
                          "".join(J[0] for J in I),
                          off/8));

    def genPrinter(self):
        x = ["def printer(self,F,offset=0,*args):",
             "    rdma.binstruct.BinStruct.printer(self,F,offset,*args);"];
        groups = list(self.mbGroup);
        I = 0;
        while I < len(groups):
            bits = sum(J[1].lenBits() for J in groups[I]);
            if bits >= 32:
                I = I + 1;
                continue;
            groups[I] = groups[I] + groups[I+1];
            del groups[I+1];

        off = 0;
        for I in groups:
            bits = sum(J[1].lenBits() for J in I);
            assert bits % 32 == 0;
            label = ','.join("%s=%%r"%(J[0]) for J in I);
            label2 = ','.join("self.%s"%(J[0]) for J in I);
            x.append('    label = "%s"%%(%s);'%(label,label2));
            x.append('    self.dump(F,%u,%u,label,offset);'%(off,off+bits));
            off = off + bits;
        if len(x) == 1:
            x.append('    return;');
        self.funcs.append(x);

    def asPython(self,F):
        self.funcs = [];

        if self.mb:
            x = ["def __init__(self,*args):"];
            for name,ty in self.mb:
                if ty.isObject():
                    x.append("    self.%s = %s;"%(name,ty.initStr()));
            if len(x) != 1:
                x.append("    rdma.binstruct.BinStruct.__init__(self,*args);");
                self.funcs.append(x);
            x = ["def zero(self):"];
            for name,ty in self.mb:
                x.append("    self.%s = %s;"%(name,ty.initStr()));
            self.funcs.append(x);

        pack = ["def pack_into(self,buffer,offset=0):"];
        unpack = ["def unpack_from(self,buffer,offset=0):",
                  "    self._buf = buffer[offset:];"];
        fmts = self.structFormat(self.mbGroup,"self.");
        if fmts:
            self.genFormats(fmts,pack,unpack);
        else:
            pack.append("    return None;");
            unpack.append("    return;");
        self.funcs.append(pack);
        self.funcs.append(unpack);

        self.genPrinter();

        self.slots = ','.join(repr(I[0]) for I in self.mb);
        print >> F, """class %(name)s(rdma.binstruct.BinStruct):
    '''%(desc)s'''
    __slots__ = (%(slots)s);"""%\
        self.__dict__;

        print >> F,"    MAD_LENGTH = %u;"%(self.size);
        if self.mgmtClass:
            print >> F,"    MAD_CLASS = 0x%x;"%(int(self.mgmtClass,0));
            print >> F,"    MAD_CLASS_VERSION = 0x%x;"%(int(self.mgmtClassVersion,0));
        if self.attributeID:
            print >> F,"    MAD_ATTRIBUTE_ID = 0x%x;"%(int(self.attributeID,0));
        if self.methods:
            for I in self.methods.split():
                print >> F,"    MAD_%s = 0x%x; # %s"%(I.upper(),globals()[methodMap[I]],
                                                      methodMap[I]);

        for I in self.funcs:
            print >> F, "   ", "\n    ".join(I);
            print >> F

parser = optparse.OptionParser(usage="%prog")
parser.add_option('-x', '--xml', dest='xml', action="append")
parser.add_option('-o', '--struct-out', dest='struct_out')
parser.add_option('-t', '--test-out', dest='test_out')
(options, args) = parser.parse_args()

structs = [];
for I in options.xml:
    with open(I,'r') as F:
        doc = ElementTree.parse(F);
        for xml in doc.findall("struct"):
            if not xml.get("containerName"):
                structs.append(Struct(xml));
structMap = dict((I.name,I) for I in structs);

with safeUpdateCtx(options.struct_out) as F:
    print >> F, "import struct,rdma.binstruct;";
    for I in structs:
        I.asPython(F);

with safeUpdateCtx(options.test_out) as F:
    print >> F,\
"""#!/usr/bin/python
import unittest,sys
import rdma.IBA as IBA;

class structs_test(unittest.TestCase):
    def test_struct_packer(self):
        test = bytearray(512);
        testr = bytes(test);"""
    for I in structs:
        print >> F,'        assert(len(test) == 512);';
        print >> F,'        IBA.%s().pack_into(test);'%(I.name);
        print >> F,'        IBA.%s().unpack_from(testr);'%(I.name);
    print >> F, "    def test_struct_printer(self):";
    for I in structs:
        print >> F,'        IBA.%s().printer(sys.stdout);'%(I.name);
    print >> F,\
"""if __name__ == '__main__':
    unittest.main()""";
