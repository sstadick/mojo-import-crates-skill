#[diplomat::bridge]
#[diplomat::abi_rename = "generic_fixture__{0}"]
mod ffi {
    pub enum ReadStatus {
        Item = 0,
        Finished = 1,
    }

    #[diplomat::attr(auto, mut_struct_ref)]
    pub struct Entry {
        pub key: usize,
        pub value: u32,
        pub status: ReadStatus,
    }

    #[diplomat::opaque_mut]
    pub struct EntryIterator(Vec<Entry>);

    impl EntryIterator {
        #[diplomat::abi_rename = "generic_fixture__entry_iterator_new"]
        pub fn new(input: &[u32]) -> Box<Self> {
            unimplemented!()
        }

        #[diplomat::abi_rename = "generic_fixture__entry_iterator_next"]
        pub fn next(&mut self, out: &mut Entry) -> ReadStatus {
            unimplemented!()
        }

        #[diplomat::abi_rename = "generic_fixture__entry_iterator_remaining"]
        pub fn remaining(&self) -> usize {
            unimplemented!()
        }
    }

    pub fn sum_values(input: &[u32]) -> u64 {
        unimplemented!()
    }

    pub fn scale_values(input: &mut [u32], factor: u32) {
        unimplemented!()
    }

    pub fn make_bytes() -> Box<[u8]> {
        unimplemented!()
    }

    pub fn consume_bytes(input: Box<[u8]>) {
        unimplemented!()
    }
}
