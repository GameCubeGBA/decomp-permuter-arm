use std::collections::VecDeque;

use serde_json::json;

use crate::port::{ReadPort, WritePort};
use crate::{ConnectClientData, Permuter, State};
use pahserver::db::UserId;
use pahserver::util::SimpleResult;

async fn client_read(
    _port: &mut ReadPort<'_>,
    _state: &State,
) -> SimpleResult<()> {
    // TODO
    Ok(())
}

async fn client_write(
    _port: &mut WritePort<'_>,
    _state: &State,
) -> SimpleResult<()> {
    // TODO
    Ok(())
}

pub(crate) async fn handle_connect_client<'a>(
    mut read_port: ReadPort<'a>,
    mut write_port: WritePort<'a>,
    _who: &UserId,
    state: &State,
    mut data: ConnectClientData,
) -> SimpleResult<()> {
    for permuter_data in &mut data.permuters {
        permuter_data.source = String::from_utf8(read_port.read_compressed().await?)?;
        permuter_data.target_o_bin = read_port.read_compressed().await?;
    }
    write_port.write_json(&json!({})).await?;

    // TODO: validate that priority is sane
    let energy_add = (data.permuters.len() as f64) / data.priority;

    let mut perm_ids = Vec::new();
    {
        let mut m = state.m.lock().unwrap();
        for permuter_data in data.permuters {
            let id = m.next_permuter_id;
            m.next_permuter_id += 1;
            perm_ids.push(id);
            m.permuters.insert(
                id,
                Permuter {
                    data: permuter_data.into(),
                    work_queue: VecDeque::new(),
                    result_queue: VecDeque::new(),
                    stale: false,
                    priority: data.priority,
                    energy_add,
                },
            );
        }
    }

    let r = tokio::try_join!(
        client_read(&mut read_port, state),
        client_write(&mut write_port, state)
    );

    {
        let mut m = state.m.lock().unwrap();
        for id in perm_ids {
            m.permuters.remove(&id);
        }
    }
    r?;
    Ok(())
}
