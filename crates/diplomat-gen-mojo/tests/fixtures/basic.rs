#[diplomat::bridge]
mod ffi {
    #[diplomat::opaque]
    pub struct Widget(u32);

    pub struct Pair {
        pub left: u32,
        pub right: usize,
    }

    impl Widget {
        pub fn new(seed: u32) -> Box<Self> {
            unimplemented!()
        }

        pub fn value(&self, delta: i32) -> u32 {
            unimplemented!()
        }

        pub fn visit(input: &[u8]) {
            unimplemented!()
        }

        pub fn owned_bytes(input: Box<[u8]>) {
            unimplemented!()
        }

        pub fn make_bytes() -> Box<[u8]> {
            unimplemented!()
        }
    }
}
