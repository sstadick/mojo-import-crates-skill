#[diplomat::bridge]
mod ffi {
    pub struct TrivialRegisterPassable {
        pub read: u8,
        pub out: u8,
    }
}
