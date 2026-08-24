#[diplomat::bridge]
mod ffi {
    pub enum Status {
        Ok = 0,
        Missing = 7,
        Failed = -3,
    }

    #[diplomat::opaque]
    pub struct Widget;

    impl Widget {
        pub fn new(enabled: bool) -> Option<Box<Self>> {
            enabled.then(|| Box::new(Self))
        }

        pub fn status(&self) -> Status {
            Status::Ok
        }

        pub fn inspect(other: Option<&Self>) -> Status {
            if other.is_some() {
                Status::Ok
            } else {
                Status::Missing
            }
        }

        pub fn unsupported_optional_scalar() -> Option<u32> {
            None
        }
    }
}
