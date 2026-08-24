#[diplomat::bridge]
mod ffi {
    #[diplomat::attr(auto, mut_struct_ref)]
    pub struct Vector2 {
        pub x: f64,
        pub y: f64,
    }

    #[diplomat::attr(auto, mut_struct_ref)]
    pub struct FloatOut {
        pub value: f64,
    }

    impl Vector2 {
        #[diplomat::abi_rename = "generic_fixture__vector_dot"]
        pub fn dot(&self, other: &Self, out: &mut FloatOut) -> u8 {
            out.value = self.x * other.x + self.y * other.y;
            0
        }
    }
}
