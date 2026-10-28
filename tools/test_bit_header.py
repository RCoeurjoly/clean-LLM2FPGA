import unittest
from tools.bit_header import normalized, fields, DATE, TIME

def fixture():
    data=b'\x00\x09'+bytes.fromhex('0ff00ff00ff00ff000')+b'\x00\x01'
    for tag,value in [(b'a',b'model.frm\0'),(b'b',b'\0'),(b'c',b'2000/01/01\0'),(b'd',b'00:00:00\0')]:
        data+=tag+len(value).to_bytes(2,'big')+value
    return data+b'e'+(4).to_bytes(4,'big')+b'\xaa\x99\x55\x66'
class HeaderTests(unittest.TestCase):
    def test_only_timestamp_changes(self):
        a=fixture();b=normalized(a);p=fields(a)
        self.assertEqual(b[slice(*p['c'])],DATE);self.assertEqual(b[slice(*p['d'])],TIME)
        for tag in ('a','b','e'):self.assertEqual(a[slice(*p[tag])],b[slice(*p[tag])])
        self.assertEqual(normalized(b),b)
    def test_corrupt_or_truncated_rejected(self):
        for value in (b'',fixture()[:-1],b'X'+fixture()[1:],fixture()+b'X'):
            with self.assertRaises(ValueError):normalized(value)
    def test_payload_difference_is_preserved(self):
        a=fixture();b=a[:-1]+b'X';self.assertNotEqual(normalized(a),normalized(b))
if __name__=='__main__':unittest.main()
